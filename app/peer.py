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
from transport.connection import Connection, ConnectionManager

logger = logging.getLogger(__name__)


class PeerError(RuntimeError):
    """Raised when a direct peer cannot be used for a private message."""


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
        """Send a plaintext DM over a direct UDP peer connection.

        `Stream.send()` queues the reliable message; its completion does NOT
        prove that the remote application has displayed the message.
        """
        if self._closed:
            raise PeerError("peer service is closed")
        if not messages.is_valid_username(to) or to == self.username:
            raise PeerError("invalid peer username")
        if not isinstance(text, str) or not text:
            raise ValueError("DM text must be non-empty")

        async with self._locks[to]:
            peer = self._lookup_peer(to)
            if peer is None:
                raise PeerError(f"peer {to!r} is not online")
            address = peer.get("addr")
            if not (isinstance(address, (tuple, list)) and len(address) == 2):
                raise PeerError(f"peer {to!r} has no usable UDP address")

            conns = self._sessions.get(to, [])
            if conns:
                conn = conns[0]
            else:
                try:
                    conn = await self.mgr.connect((str(address[0]), int(address[1])), self.username)
                except (ConnectionError, OSError) as exc:
                    raise PeerError(f"direct connection to {to!r} failed") from exc
                self._attach(to, conn)

            message = messages.make("DM", text=text, ts=int(time.time() * 1000))
            try:
                await conn.control_stream.send(messages.encode(message))
            except (ConnectionError, RuntimeError) as exc:
                raise PeerError(f"failed to send DM to {to!r}") from exc

            self._last_activity[conn] = asyncio.get_running_loop().time()
            outgoing = dict(message)
            outgoing.update({"from": self.username, "to": to})
            await self._emit({"kind": "dm", "direction": "outgoing", "message": outgoing})
            return outgoing

    def _attach(self, name: str, conn: Connection, *, reader_is_current: bool = False) -> None:
        if self._closed:
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
                self._last_activity[conn] = asyncio.get_running_loop().time()
                incoming = dict(message)
                # Never trust an arbitrary `from` property sent in the JSON.
                incoming.update({"from": name, "to": self.username})
                await self._emit({"kind": "dm", "direction": "incoming", "message": incoming})
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
