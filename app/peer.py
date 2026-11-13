"""Direct peer-to-peer sessions over the project's existing UDP transport.

Application-layer logic only: UDP sockets, reliability and Level 1 encryption
come from `transport/`. The tracker only supplies the peer's advertised UDP
address, which is a routing hint rather than authentication.

File transfer extends this module without touching DM:
    service.register_message_handler("FILE_OFFER", handler)   # and the rest
    service.set_stream_handler(on_chunk_stream)               # new data stream
    conn = await service.connection_to(peer)                  # open a stream
    await service.send_to_peer(peer, message)                 # control messages
    service.touch(conn)                                       # keep alive

No Level 2 identity proof exists yet, so HELLO.peer_name plus the roster
address is still only a routing hint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable, Optional

from app import messages
from transport.connection import Connection, ConnectionManager, ConnectionState
from transport.stream import Stream

logger = logging.getLogger(__name__)

MAX_DM_TEXT_BYTES = 4096
DM_HISTORY_PER_PEER = 100
DM_HISTORY_TOTAL = 200
ROSTER_GRACE_S = 2.0


class PeerError(RuntimeError):
    """Raised when a direct peer cannot be used for a private message."""

    def __init__(self, message: str, code: str = "send_failed"):
        super().__init__(message)
        self.code = code


PeerLookup = Callable[[str], Optional[dict]]
EventEmitter = Callable[[dict], Awaitable[None]]
MessageHandler = Callable[[Connection, str, dict], Awaitable[None]]
StreamHandler = Callable[[Connection, Stream], Awaitable[None]]


class PeerService:
    """One per ClientDaemon; manages its outgoing and incoming direct peers.

    Only the documented public transport API is used.  Connection objects are
    grouped by username because simultaneous connects may create two valid
    UDP connections; we retain them until one closes instead of racing to
    delete a connection which is still carrying a message.
    """

    def __init__(
        self,
        mgr: ConnectionManager,
        username: str,
        lookup_peer: PeerLookup,
        emit: EventEmitter,
        idle_timeout: float = 120.0,
    ) -> None:
        if not messages.is_valid_username(username):
            raise ValueError("invalid local username")
        self.mgr = mgr
        self.username = username
        self._lookup_peer = lookup_peer
        self._emit = emit
        self._sessions: dict[str, list[Connection]] = defaultdict(list)
        self._tasks: dict[Connection, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._closed = False
        if idle_timeout <= 0:
            raise ValueError("idle_timeout must be positive")
        self.idle_timeout = idle_timeout
        self._last_activity: dict[Connection, float] = {}

        # Extension points for file transfer: FILE_* control handlers and one
        # inbound data-stream handler. DM is registered by default.
        self._message_handlers: dict[str, MessageHandler] = {"DM": self._handle_dm}
        self._stream_handler: Optional[StreamHandler] = None
        self._stream_hooked: set[Connection] = set()

        # Bounded in-memory DM log. It is a UI convenience only: one process,
        # one device, no persistence, no cross-device sync.
        self._dm_log: dict[str, list[dict]] = {}
        self._dm_count = 0

        self._reaper_task = asyncio.create_task(self._reap_idle())

    @property
    def connected_peers(self) -> list[str]:
        return sorted(name for name, conns in self._sessions.items() if conns)

    def accept_incoming(self, conn: Connection) -> None:
        """Synchronous ConnectionManager callback; never await inside it."""
        name = conn.peer_name
        if self._closed or not messages.is_valid_username(name) or name == self.username:
            conn.close()
            return
        # The tracker may deliver PEER_JOINED slightly later than the direct
        # HELLO; defer the roster check briefly to handle that legitimate race.
        task = asyncio.create_task(self._authorize_incoming(conn, name))
        self._tasks[conn] = task

    async def _authorize_incoming(self, conn: Connection, name: str) -> None:
        try:
            entry = None
            attempts = int(ROSTER_GRACE_S / 0.05)
            for _ in range(attempts):  # allow PEER_JOINED to catch up
                if self._closed:
                    return
                entry = self._lookup_peer(name)
                if entry is not None:
                    break
                await asyncio.sleep(0.05)
            if entry is None:
                logger.warning("rejecting unknown inbound peer %s", name)
                conn.close()
                return

            # The roster address is a routing hint, not authentication: a
            # multi-homed or VPN peer can legitimately connect from a different
            # local address than the one the tracker observed. Level 1 has no
            # identity proof yet, so log the mismatch instead of dropping the
            # session. Level 2 will make this check cryptographic.
            roster = tuple(entry.get("addr", ()))
            direct = tuple(conn.peer_addr)
            if len(roster) == 2 and len(direct) == 2:
                if roster[0] != direct[0]:
                    logger.warning(
                        "inbound peer %s connects from %s but the roster says "
                        "%s; accepting (routing hint only)",
                        name, direct, roster,
                    )
                elif roster[1] != direct[1]:
                    logger.info(
                        "inbound peer %s port differs from roster (%s vs %s); "
                        "accepting", name, direct[1], roster[1],
                    )

            self._attach(name, conn, reader_is_current=True)
            await self._reader(conn, name)
        except asyncio.CancelledError:
            raise
        finally:
            self._forget(conn, name)
            self._tasks.pop(conn, None)

    async def send_to_peer(self, to: str, message: dict) -> None:
        """Send one validated app message over a direct peer session.

        Generic primitive for DM and files. It reuses or creates the session,
        takes the per-peer lock, and retries once on a dead connection. It does
        not emit a DM event; callers own their own bookkeeping.
        """
        if self._closed:
            raise PeerError("peer service is closed", code="send_failed")
        if not messages.is_valid_username(to) or to == self.username:
            raise PeerError("invalid peer username", code="bad_peer")
        messages.validate(message)
        async with self._locks[to]:
            await self._send_locked(to, message)

    async def connection_to(self, to: str) -> Connection:
        """Return a live direct connection to `to`, dialing if needed.

        Files use this after FILE_ACCEPT to open a data stream. The connection
        stays owned by PeerService; do not close it directly.
        """
        if self._closed:
            raise PeerError("peer service is closed", code="send_failed")
        if not messages.is_valid_username(to) or to == self.username:
            raise PeerError("invalid peer username", code="bad_peer")
        async with self._locks[to]:
            return await self._connection_locked(to)

    def register_message_handler(self, kind: str, handler: MessageHandler) -> None:
        """Files register FILE_* handlers here; DM is pre-registered."""
        self._message_handlers[kind] = handler

    def set_stream_handler(self, handler: StreamHandler) -> None:
        """Files set the handler for inbound chunk streams."""
        self._stream_handler = handler

    def touch(self, conn: Connection) -> None:
        """Mark a session active; long file transfers must call this."""
        self._last_activity[conn] = asyncio.get_running_loop().time()

    async def send_dm(self, to: str, text: str) -> dict:
        """Send a DM over a direct peer connection and record it locally.

        `Stream.send()` queues the reliable message; its completion does NOT
        prove that the remote application displayed it. There is no delivery
        ack. The message is appended to the local DM log only after it was
        queued successfully, so a failed send does not create a ghost.
        """
        if self._closed:
            raise PeerError("peer service is closed", code="send_failed")
        if not messages.is_valid_username(to) or to == self.username:
            raise PeerError("invalid peer username", code="bad_peer")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("DM text must be non-empty")
        if len(text.encode("utf-8")) > MAX_DM_TEXT_BYTES:
            raise ValueError("DM text is too long")

        # One id for both attempts, so a retry cannot be displayed twice.
        message = messages.make("DM", text=text, ts=int(time.time() * 1000))
        record = dict(message)
        record.update({
            "from": self.username,
            "to": to,
            "direction": "outgoing",
        })

        async with self._locks[to]:
            await self._send_locked(to, message)
            if self._append_dm(to, record):
                await self._emit({
                    "kind": "dm",
                    "direction": "outgoing",
                    "message": record,
                })
        return record

    async def _connection_locked(self, to: str) -> Connection:
        """Resolve/dial a live connection. Caller must hold the peer lock."""
        peer = self._lookup_peer(to)
        if peer is None:
            raise PeerError(f"peer {to!r} is not online", code="peer_offline")
        address = peer.get("addr")
        if not (isinstance(address, (tuple, list)) and len(address) == 2):
            raise PeerError(
                f"peer {to!r} has no usable UDP address", code="peer_offline"
            )
        addr = (str(address[0]), int(address[1]))

        conn = self._live_conn(to)
        if conn is not None:
            return conn

        try:
            conn = await self.mgr.connect(addr, self.username)
        except (ConnectionError, OSError) as exc:
            logger.info("direct connect to %s at %s failed: %s", to, addr, exc)
            raise PeerError(
                f"direct connection to {to!r} at {addr} failed",
                code="peer_unreachable",
            ) from exc
        if conn.state is not ConnectionState.ESTABLISHED:
            conn.close()
            raise PeerError(
                f"handshake with {to!r} did not complete",
                code="peer_unreachable",
            )
        self._attach(to, conn)
        return conn

    async def _send_locked(self, to: str, message: dict) -> None:
        """Send over the peer lock, retrying once with a fresh connection."""
        last_exc: Optional[Exception] = None
        for _ in range(2):
            conn = await self._connection_locked(to)
            try:
                await conn.control_stream.send(messages.encode(message))
            except (ConnectionError, RuntimeError) as exc:
                last_exc = exc
                logger.info(
                    "sending %s to %s over %s failed: %s",
                    message.get("t"), to, conn.peer_addr, exc,
                )
                self._forget(conn, to)
                conn.close()
                continue
            self.touch(conn)
            return
        raise PeerError(
            f"failed to send {message.get('t')} to {to!r}", code="send_failed"
        ) from last_exc

    def dm_history(self) -> dict[str, list[dict]]:
        """Return a copy of the per-peer DM log for daemon.state()."""
        return {
            peer: [dict(message) for message in messages]
            for peer, messages in self._dm_log.items()
        }

    def _append_dm(self, peer: str, record: dict) -> bool:
        """Append to the bounded log; return False for a duplicate id."""
        bucket = self._dm_log.setdefault(peer, [])
        if any(message.get("id") == record.get("id") for message in bucket):
            return False

        bucket.append(dict(record))
        self._dm_count += 1

        while len(bucket) > DM_HISTORY_PER_PEER:
            bucket.pop(0)
            self._dm_count -= 1

        while self._dm_count > DM_HISTORY_TOTAL:
            oldest_peer = min(
                (name for name, entries in self._dm_log.items() if entries),
                key=lambda name: self._dm_log[name][0].get("ts", 0),
            )
            self._dm_log[oldest_peer].pop(0)
            self._dm_count -= 1
            if not self._dm_log[oldest_peer]:
                del self._dm_log[oldest_peer]

        return True

    def _live_conn(self, name: str) -> Optional[Connection]:
        """Return a usable session; prune closed ones left by a close race."""
        dead = []
        for conn in list(self._sessions.get(name, [])):
            if (conn.state is ConnectionState.ESTABLISHED
                    and conn.control_stream is not None):
                return conn
            dead.append(conn)
        for conn in dead:
            self._forget(conn, name)
        return None

    def _attach(self, name: str, conn: Connection, *, reader_is_current: bool = False) -> None:
        if self._closed or conn.state is not ConnectionState.ESTABLISHED:
            conn.close()
            return
        if conn not in self._sessions[name]:
            self._sessions[name].append(conn)
        self._last_activity[conn] = asyncio.get_running_loop().time()
        conn.on_close(lambda closed: self._forget(closed, name))
        if conn not in self._stream_hooked:
            self._stream_hooked.add(conn)
            conn.on_stream(lambda stream: asyncio.create_task(
                self._dispatch_stream(conn, stream)))
        if not reader_is_current:
            self._tasks[conn] = asyncio.create_task(self._reader(conn, name))

    async def _reader(self, conn: Connection, name: str) -> None:
        try:
            while not self._closed:
                raw = await conn.control_stream.recv()
                try:
                    message = messages.decode(raw)
                except messages.MessageError as exc:
                    await self._emit({"kind": "error", "code": "BAD_MESSAGE", "text": str(exc)})
                    continue

                handler = self._message_handlers.get(message["t"])
                if handler is None:
                    await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                                      "text": f"unexpected peer message {message['t']!r}"})
                    continue

                self.touch(conn)
                try:
                    await handler(conn, name, message)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("peer message handler failed for %s", message["t"])
                    await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                                      "text": f"peer handler failed for {message['t']}"})
        except ConnectionError:
            pass  # remote FIN / liveness timeout / local shutdown
        except asyncio.CancelledError:
            raise
        finally:
            self._forget(conn, name)
            self._tasks.pop(conn, None)

    async def _handle_dm(self, conn: Connection, name: str, message: dict) -> None:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                              "text": "empty DM text"})
            return
        if len(text.encode("utf-8")) > MAX_DM_TEXT_BYTES:
            await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                              "text": "DM text too long"})
            return

        incoming = dict(message)
        # Never trust an arbitrary `from` property sent in the JSON.
        incoming.update({
            "from": name,
            "to": self.username,
            "direction": "incoming",
        })
        if self._append_dm(name, incoming):
            await self._emit({"kind": "dm", "direction": "incoming",
                              "message": incoming})

    async def _dispatch_stream(self, conn: Connection, stream: Stream) -> None:
        if self._closed:
            conn.close()
            return
        self.touch(conn)
        if self._stream_handler is None:
            logger.warning("unexpected inbound stream %s from %s; closing",
                           stream.stream_id, conn.peer_addr)
            conn.close()
            return
        try:
            await self._stream_handler(conn, stream)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("peer stream handler failed")
            conn.close()
        finally:
            self.touch(conn)

    def _forget(self, conn: Connection, name: str) -> None:
        self._last_activity.pop(conn, None)
        self._stream_hooked.discard(conn)
        sessions = self._sessions.get(name)
        if sessions is not None and conn in sessions:
            sessions.remove(conn)
            if not sessions:
                self._sessions.pop(name, None)

    async def _reap_idle(self) -> None:
        """Close unused app peer sessions (transport PING alone is not activity)."""
        try:
            while not self._closed:
                await asyncio.sleep(min(10.0, max(0.05, self.idle_timeout / 2)))
                now = asyncio.get_running_loop().time()
                for conn, seen in list(self._last_activity.items()):
                    if now - seen >= self.idle_timeout:
                        conn.close()
        except asyncio.CancelledError:
            raise

    def peer_left(self, name: str) -> None:
        """Tracker presence invalidates a direct session."""
        for conn in list(self._sessions.get(name, [])):
            conn.close()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reaper_task.cancel()
        await asyncio.gather(self._reaper_task, return_exceptions=True)
        for sessions in list(self._sessions.values()):
            for conn in list(sessions):
                conn.close()
        for conn in list(self._tasks):
            conn.close()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._sessions.clear()
        self._stream_hooked.clear()
