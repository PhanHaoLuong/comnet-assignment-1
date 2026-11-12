"""Smoke checks for app/messages.py plus tracker outputs.

    python3 scripts/messages_demo.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messages
from app.tracker import Tracker


class FakeConn:
    def __init__(self, port):
        self.peer_addr = ("127.0.0.1", port)


def roundtrip(msg):
    raw = messages.encode(msg)
    assert isinstance(raw, bytes)
    assert messages.decode(raw) == msg, msg
    return raw


def main():
    peer = {"username": "bob", "addr": ["127.0.0.1", 4002]}
    history = [{"gseq": 1, "from": "bob", "text": "hi", "ts": 1731234567000}]
    sha = "a" * 64

    samples = [
        messages.make("LOGIN", username="alice", password="pw"),
        messages.make("LOGIN_OK", peers=[peer], history=history,
                      your_addr=["127.0.0.1", 4001]),
        messages.make("LOGIN_ERR", reason="bad_credentials"),
        messages.make("PEER_JOINED", username="bob", addr=["127.0.0.1", 4002]),
        messages.make("PEER_LEFT", username="bob"),
        messages.make("CHAT_SEND", text="hello"),
        messages.make("CHAT", gseq=1, **{"from": "alice"}, text="hello",
                      ts=1731234567000),
        messages.make("CONNECT_REQ", target="bob"),
        messages.make("INTRO", username="bob", addr=["127.0.0.1", 4002]),
        messages.make("RELAY", to="bob", payload={"t": "DM"}),
        messages.make("LOGOUT"),
        messages.make("ERROR", code="BAD_MESSAGE", text="nope"),
        messages.make("DM", text="hi", ts=1731234567000),
        messages.make("FILE_OFFER", transfer_id="t1", name="a.txt", size=3,
                      sha256=sha, stream_id=2),
        messages.make("FILE_ACCEPT", transfer_id="t1"),
        messages.make("FILE_REJECT", transfer_id="t1", reason="no"),
        messages.make("FILE_END", transfer_id="t1", total_bytes=3, sha256=sha),
        messages.make("FILE_DONE", transfer_id="t1", ok=True),
        messages.make("FILE_ABORT", transfer_id="t1", reason="gone"),
    ]
    for sample in samples:
        roundtrip(sample)
    print(f"PASS {len(samples)} message types round-trip")

    # The tracker's own output must satisfy the same schema.
    tracker = Tracker()
    alice = tracker.add(FakeConn(4001))
    bob = tracker.add(FakeConn(4002))
    outgoing = tracker.handle_login(alice, "alice", True, 1.0)
    outgoing += tracker.handle_login(bob, "bob", True, 1.1)
    outgoing += tracker.handle(alice, {"t": "CHAT_SEND", "text": "hello"}, 2.0)
    outgoing += tracker.disconnected(bob, 3.0)
    for _session, msg in outgoing:
        roundtrip(msg)
    print(f"PASS {len(outgoing)} tracker outputs satisfy the schema")

    # Rejection cases.
    bad = [
        lambda: messages.encode({"t": "LOGOUT"}),                     # no id
        lambda: messages.encode({"t": "NOPE", "id": "m1"}),           # unknown t
        lambda: messages.make("CHAT", gseq=True, **{"from": "alice"},
                              text="x", ts=1),                        # bool int
        lambda: messages.make("LOGIN", username="Alice", password="pw"),
        lambda: messages.make("LOGIN_OK", peers=[{"username": "bob"}],
                              history=[], your_addr=["127.0.0.1", 1]),
        lambda: messages.make("ERROR", code="NOPE", text="x"),
        lambda: messages.decode(b"not json"),
        lambda: messages.decode(b'{"t":"CHAT","id":"m1","gseq":1,'
                                b'"from":"bob","text":"x","ts":NaN}'),
    ]
    for index, call in enumerate(bad, start=1):
        try:
            call()
        except messages.MessageError:
            continue
        raise AssertionError(f"case {index} was not rejected")
    print(f"PASS {len(bad)} invalid messages rejected")

    # Extra fields are tolerated for forward compatibility.
    msg = messages.make("LOGOUT", future_field=1)
    assert messages.decode(messages.encode(msg)) == msg
    print("PASS unknown extra fields are preserved")

    # Tracker-generated ids are accepted.
    assert all(len(msg["id"]) <= messages.MAX_ID_LEN for _, msg in outgoing)
    print("PASS tracker ids accepted")


if __name__ == "__main__":
    main()
