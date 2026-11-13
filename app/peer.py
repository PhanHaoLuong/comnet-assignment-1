"""Direct peer-to-peer sessions over the project's existing UDP transport.

This is application-layer logic only.  It deliberately does NOT implement UDP
sockets, reliability, encryption, discovery, or tracker routing.  The tracker
provides the peer's advertised UDP address; ConnectionManager provides a
reliable message-oriented control stream on the direct peer connection.

No authentication/crypto guarantee is made here: HELLO.peer_name and the
tracker's addresses are only routing hints until the crypto track is wired in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Awaitable, Callable, Optional

from app import messages
from transport.connection import Connection, ConnectionManager, ConnectionState

logger = logging.getLogger(__name__)

MAX_DM_TEXT_BYTES = 4096
DM_HISTORY_PER_PEER = 100
DM_HISTORY_TOTAL = 200


class PeerError(RuntimeError):
    """Raised when a direct peer cannot be used for a private message."""

    def __init__(self, message: str, code: str = "send_failed"):
        super().__init__(message)
        self.code = code


PeerLookup = Callable[[str], Optional[dict]]
EventEmitter = Callable[[dict], Awaitable[None]]


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
            for _ in range(20):  # up to 1 s for PEER_JOINED to reach the daemon
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
            # The tracker reports the peer's UDP endpoint.  Require exact
            # address/port for this simple LAN demo; it is not cryptographic
            # authentication and does not solve NAT traversal.
            if tuple(entry.get("addr", ())) != tuple(conn.peer_addr):
                logger.warning("rejecting inbound peer %s: endpoint mismatch", name)
                conn.close()
                return
            self._attach(name, conn, reader_is_current=True)
            await self._reader(conn, name)
        except asyncio.CancelledError:
            raise
        finally:
            self._forget(conn, name)
            self._tasks.pop(conn, None)

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

        async with self._locks[to]:
            peer = self._lookup_peer(to)
            if peer is None:
                raise PeerError(f"peer {to!r} is not online", code="peer_offline")
            address = peer.get("addr")
            if not (isinstance(address, (tuple, list)) and len(address) == 2):
                raise PeerError(
                    f"peer {to!r} has no usable UDP address", code="peer_offline"
                )
            addr = (str(address[0]), int(address[1]))

            # One id for both attempts, so a retry cannot be displayed twice.
            message = messages.make("DM", text=text, ts=int(time.time() * 1000))
            record = dict(message)
            record.update({
                "from": self.username,
                "to": to,
                "direction": "outgoing",
            })

            last_exc: Optional[Exception] = None
            for _ in range(2):
                conn = self._live_conn(to)
                if conn is None:
                    try:
                        conn = await self.mgr.connect(addr, self.username)
                    except (ConnectionError, OSError) as exc:
                        last_exc = exc
                        break
                    if conn.state is not ConnectionState.ESTABLISHED:
                        conn.close()
                        last_exc = ConnectionError("handshake did not complete")
                        break
                    self._attach(to, conn)

                try:
                    await conn.control_stream.send(messages.encode(message))
                except (ConnectionError, RuntimeError) as exc:
                    last_exc = exc
                    self._forget(conn, to)
                    conn.close()
                    continue

                self._last_activity[conn] = asyncio.get_running_loop().time()
                if self._append_dm(to, record):
                    await self._emit({
                        "kind": "dm",
                        "direction": "outgoing",
                        "message": record,
                    })
                return record

            if isinstance(last_exc, (ConnectionError, OSError)):
                raise PeerError(
                    f"direct connection to {to!r} failed", code="peer_unreachable"
                ) from last_exc
            raise PeerError(
                f"failed to send DM to {to!r}", code="send_failed"
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
                if message["t"] != "DM":
                    await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                                      "text": f"unexpected peer message {message['t']!r}"})
                    continue

                text = message.get("text")
                if not isinstance(text, str) or not text.strip():
                    await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                                      "text": "empty DM text"})
                    continue
                if len(text.encode("utf-8")) > MAX_DM_TEXT_BYTES:
                    await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                                      "text": "DM text too long"})
                    continue

                self._last_activity[conn] = asyncio.get_running_loop().time()
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
        except ConnectionError:
            pass  # remote FIN / liveness timeout / local shutdown
        except asyncio.CancelledError:
            raise
        finally:
            self._forget(conn, name)
            self._tasks.pop(conn, None)

    def _forget(self, conn: Connection, name: str) -> None:
        self._last_activity.pop(conn, None)
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
