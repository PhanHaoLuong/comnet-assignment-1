"""Tracker-side application state machine: roster, presence, chat ordering.

This module does no I/O. The daemon decodes a message, calls one of these
methods, and sends the returned (session, message) pairs on each session's
control stream.

    tracker = Tracker()
    session = tracker.add(conn)
    outgoing = tracker.handle_login(session, username, ok, now)
    ...
    outgoing = tracker.handle(session, msg, now)
    outgoing = tracker.disconnected(session, now)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from transport.connection import Connection


@dataclass
class Session:
    conn: Connection
    username: str | None = None
    authenticated: bool = False
    attempts: int = 0
    last_seen: float = 0.0


class Tracker:
    def __init__(self, history_limit: int = 50):
        self.by_username: dict[str, Session] = {}
        self.by_conn: dict[Connection, Session] = {}
        self.history: deque[dict] = deque(maxlen=history_limit)
        self.gseq: int = 0
        self._next_message_id: int = 0

    # ------------------------------------------------------------------ setup

    def add(self, conn: Connection) -> Session:
        session = Session(conn=conn)
        self.by_conn[conn] = session
        return session

    # ------------------------------------------------------------------ login

    def handle_login(
        self, session: Session, username: str, ok: bool, now: float
    ) -> list[tuple[Session, dict]]:
        if session.authenticated:
            return [(session, self._msg("LOGIN_ERR", reason="already_authenticated"))]

        session.last_seen = now

        if not ok:
            session.attempts += 1
            return [(session, self._msg("LOGIN_ERR", reason="bad_credentials"))]

        if username in self.by_username:
            # Do not count this as a credential failure.
            return [(session, self._msg("LOGIN_ERR", reason="name_taken"))]

        session.username = username
        session.authenticated = True
        session.attempts = 0
        self.by_username[username] = session

        peers = [self._peer(other) for other in self.by_username.values()
                 if other is not session]

        outgoing: list[tuple[Session, dict]] = [
            (session, self._msg(
                "LOGIN_OK",
                peers=peers,
                history=list(self.history),
                your_addr=list(session.conn.peer_addr),
            ))
        ]

        # LOGIN_OK goes first; then everyone else learns about the new user.
        joined = self._msg(
            "PEER_JOINED",
            username=username,
            addr=list(session.conn.peer_addr),
        )
        for other in self._authenticated():
            if other is not session:
                outgoing.append((other, joined))
        return outgoing

    # --------------------------------------------------------------- post-login

    def handle(
        self, session: Session, msg: dict, now: float
    ) -> list[tuple[Session, dict]]:
        if not session.authenticated:
            return [(session, self._msg(
                "ERROR", code="NOT_AUTHENTICATED", text="log in first"
            ))]

        session.last_seen = now
        kind = msg.get("t")

        if kind == "LOGOUT":
            return self.disconnected(session, now)

        if kind == "CHAT_SEND":
            text = msg.get("text")
            if not isinstance(text, str) or not text:
                return [(session, self._msg(
                    "ERROR", code="BAD_MESSAGE", text="empty chat text"
                ))]

            self.gseq += 1
            chat = self._msg(
                "CHAT",
                gseq=self.gseq,
                text=text,
                ts=int(time.time() * 1000),
            )
            chat["from"] = session.username

            self.history.append({
                "gseq": chat["gseq"],
                "from": chat["from"],
                "text": chat["text"],
                "ts": chat["ts"],
            })
            return [(target, chat) for target in self._authenticated()]

        return [(session, self._msg(
            "ERROR", code="BAD_MESSAGE", text=f"unknown message {kind!r}"
        ))]

    # ----------------------------------------------------------------- teardown

    def disconnected(
        self, session: Session, now: float
    ) -> list[tuple[Session, dict]]:
        # Idempotent: LOGOUT and on_close may both call this.
        if self.by_conn.pop(session.conn, None) is None:
            return []

        session.last_seen = now
        username = session.username
        was_authenticated = session.authenticated
        session.authenticated = False
        session.username = None

        if username is not None:
            self.by_username.pop(username, None)

        if not was_authenticated or username is None:
            return []

        left = self._msg("PEER_LEFT", username=username)
        return [(other, left) for other in self._authenticated()]

    # ------------------------------------------------------------------ helpers

    def _authenticated(self) -> list[Session]:
        return [session for session in self.by_username.values()
                if session.authenticated]

    def _peer(self, session: Session) -> dict:
        return {
            "username": session.username,
            "addr": list(session.conn.peer_addr),
        }

    def _msg(self, kind: str, **fields) -> dict:
        self._next_message_id += 1
        message = {"t": kind, "id": f"m{self._next_message_id}"}
        message.update(fields)
        return message
