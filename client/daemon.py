"""Per-user client daemon: tracker connection, login, peers, chat, direct DMs.

No HTTP, no web code, no file transfers yet. The gateway consumes `events` and
calls the public methods.

    python3 -m client.daemon --tracker 127.0.0.1:6767 --username alice
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from typing import Optional

from app import messages
from common.constants import PEER_DEAD_S, PING_INTERVAL_S
from transport.connection import Connection, ConnectionManager, ConnectionState

from app.peer import PeerService

logger = logging.getLogger(__name__)


class LoginError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ClientDaemon:
    def __init__(
        self,
        tracker_addr: tuple[str, int],
        *,
        listen_host: str = "0.0.0.0",
        listen_port: int = 0,
        ping_interval: float = PING_INTERVAL_S,
        dead_timeout: float = PEER_DEAD_S,
        history_limit: int = 200,
    ):
        self.tracker_addr = tracker_addr
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.history_limit = history_limit

        self.mgr = ConnectionManager(
            on_connection=self._on_peer_connection,
            ping_interval=ping_interval,
            dead_timeout=dead_timeout,
        )
        self.tracker: Optional[Connection] = None
        self.username: Optional[str] = None
        self.peers: dict[str, dict] = {}
        self.history: list[dict] = []
        self.status = "idle"       # idle | connecting | authenticated | disconnected
        self.events: asyncio.Queue = asyncio.Queue()

        self._read_task: Optional[asyncio.Task] = None
        self._stopping = False

        self.peer_service: Optional[PeerService] = None

    # ------------------------------------------------------------------ setup

    @property
    def address(self) -> Optional[tuple[str, int]]:
        return self.mgr.address

    async def start(self) -> None:
        """Bind the local UDP endpoint. Login is a separate step."""
        if self.mgr.endpoint is None:
            await self.mgr.listen(self.listen_host, self.listen_port)
            logger.info("client listening on %s", self.mgr.address)
            if self.listen_host in ("127.0.0.1", "localhost", "::1"):
                logger.warning(
                    "client is bound to loopback only; peers on other devices "
                    "cannot connect. Use --listen-host 0.0.0.0 for a LAN demo."
                )

    async def login(self, username: str, password: str) -> dict:
        """Connect to the tracker and perform the app-level LOGIN.

        A failed LOGIN keeps the tracker connection open so the user can retry
        on the same session. The tracker counts the 3 attempts and closes it.
        """
        if self.status == "authenticated":
            raise RuntimeError("already logged in on this daemon")

        name = username.strip().lower()
        if not messages.is_valid_username(name):
            raise LoginError("bad_username")

        await self.start()
        await self._set_status("connecting", error=None)

        conn = self.tracker
        if conn is not None and conn.state is not ConnectionState.ESTABLISHED:
            conn.close()
            conn = None
            self.tracker = None

        if conn is None:
            try:
                conn = await self.mgr.connect(self.tracker_addr, name)
            except Exception as exc:
                await self._set_status("disconnected", error=str(exc))
                raise LoginError("tracker_unreachable") from exc

            self.tracker = conn
            conn.on_close(self._on_tracker_close)
            conn.on_stream(self._on_tracker_stream)

        try:
            await conn.control_stream.send(messages.encode(
                messages.make("LOGIN", username=name, password=password)
            ))
        except (ConnectionError, RuntimeError) as exc:
            self.tracker = None
            conn.close()
            await self._set_status("disconnected", error="connection lost")
            raise LoginError("connection_lost") from exc

        try:
            raw = await asyncio.wait_for(conn.control_stream.recv(), timeout=10)
        except asyncio.TimeoutError as exc:
            self.tracker = None
            conn.close()
            await self._set_status("disconnected", error="login timeout")
            raise LoginError("login_timeout") from exc
        except ConnectionError as exc:
            self.tracker = None
            conn.close()
            await self._set_status("disconnected", error="connection lost")
            raise LoginError("connection_lost") from exc

        try:
            reply = messages.decode(raw)
        except messages.MessageError as exc:
            self.tracker = None
            conn.close()
            await self._set_status("error", error=str(exc))
            raise LoginError("bad_reply") from exc

        if reply["t"] == "LOGIN_ERR":
            # Keep the connection: retry on the same session so the tracker's
            # attempt counter works. After the third failure it closes.
            await self._set_status("error", error=reply["reason"])
            raise LoginError(reply["reason"])
        if reply["t"] != "LOGIN_OK":
            self.tracker = None
            conn.close()
            await self._set_status("error", error=f"unexpected {reply['t']}")
            raise LoginError("unexpected_reply")

        if self.peer_service is not None and self.username != name:
            await self.peer_service.stop()
            self.peer_service = None

        self.username = name
        if self.peer_service is None:
            self.peer_service = PeerService(
                self.mgr, name, lookup_peer=lambda peer: self.peers.get(peer),
                emit=self._emit,
            )
        self.history = list(reply["history"])[-self.history_limit:]
        self.peers = {peer["username"]: peer for peer in reply["peers"]}
        await self._set_status("authenticated", error=None)
        await self._emit({"kind": "peers", "peers": self.peers})
        await self._emit({"kind": "history", "history": self.history})

        self._read_task = asyncio.create_task(self._read_tracker())
        return reply

    # --------------------------------------------------------------- the loop

    async def _read_tracker(self) -> None:
        try:
            while True:
                raw = await self.tracker.control_stream.recv()
                msg = messages.decode(raw)
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            await self._on_tracker_gone()
        except messages.MessageError as exc:
            await self._emit({"kind": "error", "code": "BAD_MESSAGE",
                              "text": str(exc)})

    async def _dispatch(self, msg: dict) -> None:
        kind = msg["t"]

        if kind == "PEER_JOINED":
            self.peers[msg["username"]] = {
                "username": msg["username"],
                "addr": msg["addr"],
            }
            if "identity_pub" in msg:
                self.peers[msg["username"]]["identity_pub"] = msg["identity_pub"]
            await self._emit({"kind": "peers", "peers": self.peers})

        elif kind == "PEER_LEFT":
            self.peers.pop(msg["username"], None)
            if self.peer_service is not None:
                self.peer_service.peer_left(msg["username"])

            await self._emit({"kind": "peers", "peers": self.peers})

        elif kind == "CHAT":
            self.history.append({
                "gseq": msg["gseq"],
                "from": msg["from"],
                "text": msg["text"],
                "ts": msg["ts"],
            })
            self.history = self.history[-self.history_limit:]
            await self._emit({"kind": "chat", "message": msg})

        elif kind == "DM":
            await self._emit({"kind": "dm", "message": msg})

        elif kind == "ERROR":
            await self._emit({"kind": "error", "code": msg["code"],
                              "text": msg["text"]})

        else:
            logger.debug("ignoring message type %s", kind)

    # -------------------------------------------------------------- public API

    async def send_chat(self, text: str) -> None:
        if self.tracker is None or self.status != "authenticated":
            raise RuntimeError("not logged in")
        if not isinstance(text, str) or not text:
            raise ValueError("empty chat text")
        await self.tracker.control_stream.send(messages.encode(
            messages.make("CHAT_SEND", text=text)
        ))

    async def send_dm(self, to: str, text: str) -> dict:
        """Send a direct DM; works over an existing session after tracker loss."""
        if self.peer_service is None:
            raise RuntimeError("not logged in")
        return await self.peer_service.send_dm(to, text)


    def state(self) -> dict:
        return {
            "status": self.status,
            "username": self.username,
            "tracker": list(self.tracker_addr),
            "peers": self.peers,
            "history": self.history,
            "dms": self.peer_service.dm_history()
            if self.peer_service is not None else {},
        }

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True

        if self._read_task is not None:
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)
            self._read_task = None

        if self.peer_service is not None:
            await self.peer_service.stop()
            self.peer_service = None

        if self.tracker is not None:
            self.tracker.close()
            self.tracker = None

        if self.mgr.endpoint is not None:
            self.mgr.endpoint.close()

        await self._set_status("disconnected", error=None)

    # ------------------------------------------------------------------ events

    async def _emit(self, event: dict) -> None:
        await self.events.put(event)

    async def _set_status(self, status: str, *, error: Optional[str]) -> None:
        self.status = status
        event = {"kind": "status", "status": status, "username": self.username}
        if error is not None:
            event["error"] = error
        await self._emit(event)

    # --------------------------------------------------------------- callbacks

    def _on_tracker_close(self, conn: Connection) -> None:
        if self._stopping or self.tracker is None:
            return
        asyncio.create_task(self._on_tracker_gone())

    async def _on_tracker_gone(self) -> None:
        if self._stopping or self.status == "disconnected":
            return
        self.tracker = None
        # Keep PeerService alive: direct DM/file sessions do not depend on the
        # tracker once established, and the retained roster still lets us dial
        # known peers. Chat and new peer discovery do need the tracker.
        await self._set_status("disconnected", error="tracker closed")

    def _on_tracker_stream(self, stream) -> None:
        # The tracker only uses stream 0 today; reserved for future use.
        logger.debug("tracker opened stream %s", stream.stream_id)

    def _on_peer_connection(self, conn: Connection) -> None:
        # Direct sessions survive tracker loss; only require a logged-in daemon.
        if self.peer_service is None:
            conn.close()
            return
        self.peer_service.accept_incoming(conn)


# ------------------------------------------------------------------------ CLI

def _parse_addr(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("expected host:port")
    return host, int(port)


async def _amain(args) -> None:
    daemon = ClientDaemon(
        args.tracker,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
    )
    await daemon.start()

    gateway = None
    if not args.no_gateway:
        from client.gateway import Gateway   # local import avoids a cycle

        gateway = Gateway(daemon, host=args.gateway_host, port=args.gateway_port)
        await gateway.start()
        print(f"web UI: http://{gateway.address[0]}:{gateway.address[1]}/")

    try:
        if args.username:
            try:
                await daemon.login(args.username, args.password)
                print("logged in:", daemon.state())
            except LoginError as exc:
                print(f"login failed: {exc.reason}", file=sys.stderr)
                if gateway is not None:
                    print(
                        "start without --username to log in through the web UI",
                        file=sys.stderr,
                    )
                return

        if gateway is not None:
            await asyncio.Event().wait()     # run until Ctrl-C
        else:
            while True:
                event = await daemon.events.get()
                print("event:", event)
    finally:
        if gateway is not None:
            await gateway.stop()
        await daemon.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracker", required=True, type=_parse_addr)
    parser.add_argument("--username",
                        help="omit to log in through the web UI")
    parser.add_argument("--password",
                        help="omit to be prompted (keeps it out of shell history)")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("--gateway-host", default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=8080)
    parser.add_argument("--no-gateway", action="store_true")
    args = parser.parse_args()

    if args.username and args.password is None:
        args.password = getpass.getpass("Password: ")
    if not args.username and args.no_gateway:
        parser.error("--username is required when --no-gateway is used")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
