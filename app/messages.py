"""Application protocol codec: UTF-8 JSON messages on top of a Stream.

Every message is an object with:
    t        message type (required)
    id       message id, non-empty string <= 64 chars (required)
    reply_to optional id of the message being answered

The transport only moves bytes; this module owns the message shapes.
No I/O, no asyncio, no Connection import.
"""

from __future__ import annotations

import json
import re
import uuid

MAX_MESSAGE_BYTES = 256 * 1024
MAX_ID_LEN = 64

USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
ERROR_CODES = frozenset({
    "BAD_CREDENTIALS",
    "NAME_TAKEN",
    "NOT_AUTHENTICATED",
    "UNKNOWN_PEER",
    "BAD_MESSAGE",
})


class MessageError(ValueError):
    pass


# --------------------------------------------------------------------- checks

def is_valid_username(value) -> bool:
    return isinstance(value, str) and USERNAME_RE.match(value) is not None


def _is_str(value) -> bool:
    return isinstance(value, str)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bool(value) -> bool:
    return isinstance(value, bool)


def _is_dict(value) -> bool:
    return isinstance(value, dict)


def _is_id(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= MAX_ID_LEN


def _is_username(value) -> bool:
    return is_valid_username(value)


def _is_gseq(value) -> bool:
    return _is_int(value) and value >= 0


def _is_ts(value) -> bool:
    return _is_int(value) and value >= 0


def _is_error_code(value) -> bool:
    return isinstance(value, str) and value in ERROR_CODES


def _is_addr(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and _is_int(value[1])
        and 0 <= value[1] <= 65535
    )


def _is_sha256(value) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in value)


def _is_peer(value) -> bool:
    if not isinstance(value, dict):
        return False
    if not _is_username(value.get("username")):
        return False
    if not _is_addr(value.get("addr")):
        return False
    if "identity_pub" in value and not _is_str(value["identity_pub"]):
        return False
    return True


def _is_peers(value) -> bool:
    return isinstance(value, list) and all(_is_peer(peer) for peer in value)


def _is_history_entry(value) -> bool:
    return (
        isinstance(value, dict)
        and _is_gseq(value.get("gseq"))
        and _is_username(value.get("from"))
        and _is_str(value.get("text"))
        and _is_ts(value.get("ts"))
    )


def _is_history(value) -> bool:
    return isinstance(value, list) and all(_is_history_entry(item) for item in value)


# ----------------------------------------------------------------------- specs
# (required, optional) field -> checker

SPECS = {
    "LOGIN": (
        {"username": _is_username, "password": _is_str},
        {"identity_pub": _is_str},
    ),
    "LOGIN_OK": (
        {"peers": _is_peers, "history": _is_history, "your_addr": _is_addr},
        {},
    ),
    "LOGIN_ERR": ({"reason": _is_str}, {}),
    "PEER_JOINED": (
        {"username": _is_username, "addr": _is_addr},
        {"identity_pub": _is_str},
    ),
    "PEER_LEFT": ({"username": _is_username}, {}),
    "CHAT_SEND": ({"text": _is_str}, {}),
    "CHAT": (
        {"gseq": _is_gseq, "from": _is_username, "text": _is_str, "ts": _is_ts},
        {},
    ),
    "CONNECT_REQ": ({"target": _is_username}, {}),
    "INTRO": (
        {"username": _is_username, "addr": _is_addr},
        {"identity_pub": _is_str},
    ),
    "RELAY": ({"to": _is_username, "payload": _is_dict}, {}),
    "LOGOUT": ({}, {}),
    "ERROR": ({"code": _is_error_code, "text": _is_str}, {}),
    "DM": ({"text": _is_str, "ts": _is_ts}, {}),
    "FILE_OFFER": (
        {
            "transfer_id": _is_str,
            "name": _is_str,
            "size": _is_int,
            "sha256": _is_sha256,
            "stream_id": _is_int,
        },
        {},
    ),
    "FILE_ACCEPT": ({"transfer_id": _is_str}, {}),
    "FILE_REJECT": ({"transfer_id": _is_str, "reason": _is_str}, {}),
    "FILE_END": (
        {"transfer_id": _is_str, "total_bytes": _is_int, "sha256": _is_sha256},
        {},
    ),
    "FILE_DONE": (
        {"transfer_id": _is_str, "ok": _is_bool},
        {"reason": _is_str},
    ),
    "FILE_ABORT": ({"transfer_id": _is_str, "reason": _is_str}, {}),
}

BASE_REQUIRED = {"t": _is_str, "id": _is_id}
BASE_OPTIONAL = {"reply_to": _is_id}


# ------------------------------------------------------------------ validating

def _check_fields(label, obj, required, optional):
    for field, check in required.items():
        if field not in obj:
            raise MessageError(f"{label} is missing field {field!r}")
        if not check(obj[field]):
            raise MessageError(f"{label}.{field} is invalid")
    for field, check in optional.items():
        if field in obj and not check(obj[field]):
            raise MessageError(f"{label}.{field} is invalid")


def validate(msg) -> None:
    if not isinstance(msg, dict):
        raise MessageError("message must be a JSON object")
    _check_fields("message", msg, BASE_REQUIRED, BASE_OPTIONAL)
    spec = SPECS.get(msg["t"])
    if spec is None:
        raise MessageError(f"unknown message type {msg['t']!r}")
    _check_fields(msg["t"], msg, *spec)


# -------------------------------------------------------------------- encoding

def new_id() -> str:
    return uuid.uuid4().hex[:16]


def make(kind: str, **fields) -> dict:
    """Build and validate an outgoing message with a fresh id."""
    if "t" in fields or "id" in fields:
        raise MessageError("pass the type and id implicitly")
    msg = {"t": kind, "id": new_id()}
    msg.update(fields)
    validate(msg)
    return msg


def _reject_constant(value):
    raise MessageError(f"invalid JSON constant {value}")


def encode(msg: dict) -> bytes:
    validate(msg)
    try:
        raw = json.dumps(
            msg,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MessageError(f"cannot encode message: {exc}") from exc
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MessageError(f"message too large ({len(raw)} bytes)")
    return raw


def decode(data: bytes) -> dict:
    if len(data) > MAX_MESSAGE_BYTES:
        raise MessageError(f"message too large ({len(data)} bytes)")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MessageError("message is not valid UTF-8") from exc
    try:
        msg = json.loads(text, parse_constant=_reject_constant)
    except MessageError:
        raise
    except json.JSONDecodeError as exc:
        raise MessageError(f"message is not valid JSON: {exc}") from exc
    validate(msg)
    return msg
