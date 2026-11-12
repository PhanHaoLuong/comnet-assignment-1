"""Tracker daemon: owns the socket loop, calls the pure Tracker.

    python3 -m app.tracker_server --port 6767
    python3 -m app.tracker_server --dev-auth

The state machine in app/tracker.py does not know about sockets, asyncio,
messages, or auth. This file is the only place that connects them.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import time
from typing import Awaitable, Callable, Optional, Union

from app import messages
from app.tracker import Session, Tracker
from common.constants import PEER_DEAD_S, PING_INTERVAL_S, UNAUTH_TIMEOUT_S
from transport.connection import Connection, ConnectionManager

logger = logging.getLogger(__name__)

AuthResult = Union[bool, Awaitable[bool]]
Authenticator = Callable[[str, str], AuthResult]


class TrackerServer:
    def __init__(
        self,
        authenticate: Authenticator,
        *,
        host: str = "0.0.0.0",
        port: int = 6767,
        history_limit: int = 50,
        ping_interval: float = PING_INTERVAL_S,
        dead_timeout: float = PEER_DEAD_S,
    ):
        self.authenticate = authenticate
        self.host = host
        self.port = port
        self.tracker = Tracker(history_limit)
        self.mgr = ConnectionManager(
            on_connection=self.on_connection,
            ping_interval=ping_interval,
            dead_timeout=dead_timeout,
        )
        self._sweeper: Optional[asyncio.Task] = None

    @property
    def address(self):
        return self.mgr.address

    async def start(self) -> None:
        await self.mgr.listen(self.host, self.port)
        self._sweeper = asyncio.create_task(self._sweep())
        logger.info("tracker listening on %s", self.address)

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        if self.mgr.endpoint is not None:
            self.mgr.endpoint.close()

    # -------------------------------------------------------------- connections

    def on_connection(self, conn: Connection) -> None:
        session = self.tracker.add(conn)
        session.last_seen = time.monotonic()
        conn.on_close(lambda c: asyncio.create_task(self._drop(session)))
        asyncio.create_task(self._serve(session))

    async def _serve(self, session: Session) -> None:
        try:
            while True:
                raw = await session.conn.control_stream.recv()

                try:
                    msg = messages.decode(raw)
                except messages.MessageError as exc:
                    await self._send(session, messages.make(
                        "ERROR", code="BAD_MESSAGE", text=str(exc)
                    ))
                    continue

                session.last_seen = time.monotonic()
                kind = msg["t"]

                if kind == "LOGIN":
                    outgoing = await self._login(session, msg)
                else:
                    outgoing = self.tracker.handle(
                        session, msg, session.last_seen
                    )

                await self._deliver(outgoing)

                if kind == "LOGOUT" or (
                    not session.authenticated and session.attempts >= 3
                ):
                    session.conn.close()
                    break
        except ConnectionError:
            pass
        finally:
            await self._drop(session)

    async def _login(self, session: Session, msg: dict):
        username = msg["username"]          # codec already checked lowercase
        try:
            result = self.authenticate(username, msg["password"])
            if inspect.isawaitable(result):
                result = await result
            ok = bool(result)
        except Exception:
            logger.exception("authentication failed for %s", username)
            ok = False
        return self.tracker.handle_login(
            session, username, ok, session.last_seen
        )

    # ----------------------------------------------------------------- delivery

    async def _deliver(self, outgoing) -> None:
        for target, message in outgoing:
            await self._send(target, message)

    async def _send(self, session: Session, message: dict) -> None:
        try:
            await session.conn.control_stream.send(messages.encode(message))
        except (ConnectionError, RuntimeError):
            # The peer went away while we were broadcasting; its own on_close
            # path will remove it.
            pass

    async def _drop(self, session: Session) -> None:
        outgoing = self.tracker.disconnected(session, time.monotonic())
        await self._deliver(outgoing)

    # ------------------------------------------------------------------ sweeper

    async def _sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                now = time.monotonic()
                for session in list(self.tracker.by_conn.values()):
                    if (
                        not session.authenticated
                        and now - session.last_seen > UNAUTH_TIMEOUT_S
                    ):
                        logger.debug("closing idle unauthenticated session")
                        session.conn.close()
        except asyncio.CancelledError:
            raise


# ------------------------------------------------------------------------ main

def _resolve_auth(dev_auth: bool) -> Authenticator:
    if not dev_auth:
        try:
            from app.auth import authenticate  # type: ignore
            return authenticate
        except ImportError:
            logger.warning(
                "app.auth not found; falling back to dev auth (password == 'pw')"
            )

    async def dev_authenticate(username: str, password: str) -> bool:
        return password == "pw"

    return dev_authenticate


async def _amain(args) -> None:
    server = TrackerServer(
        _resolve_auth(args.dev_auth),
        host=args.host,
        port=args.port,
        ping_interval=args.ping_interval,
        dead_timeout=args.dead_timeout,
    )
    await server.start()
    try:
        await asyncio.Event().wait()       # run until cancelled
    finally:
        await server.stop()
        logger.info("tracker stopped")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=6767)
    parser.add_argument("--dev-auth", action="store_true",
                        help="accept any user with password 'pw'")
    parser.add_argument("--ping-interval", type=float, default=PING_INTERVAL_S)
    parser.add_argument("--dead-timeout", type=float, default=PEER_DEAD_S)
    args = parser.parse_args()

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
