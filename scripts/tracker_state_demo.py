"""Pure state-machine checks for app/tracker.py (no sockets, no asyncio).

    python3 scripts/tracker_state_demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tracker import Tracker


class FakeConn:
    def __init__(self, port):
        self.peer_addr = ("127.0.0.1", port)


def kinds(outgoing):
    return [msg["t"] for _, msg in outgoing]


def main():
    tracker = Tracker()

    # --- first login -----------------------------------------------------
    alice_conn = FakeConn(4001)
    alice = tracker.add(alice_conn)
    out = tracker.handle_login(alice, "alice", True, now=1.0)
    assert kinds(out) == ["LOGIN_OK"], kinds(out)
    assert out[0][1]["peers"] == []
    assert out[0][1]["history"] == []
    assert out[0][1]["your_addr"] == ["127.0.0.1", 4001]
    print("PASS first login: LOGIN_OK only")

    # --- second login: snapshot + broadcast ------------------------------
    bob_conn = FakeConn(4002)
    bob = tracker.add(bob_conn)
    out = tracker.handle_login(bob, "bob", True, now=1.1)
    assert kinds(out) == ["LOGIN_OK", "PEER_JOINED"], kinds(out)
    assert out[0][0] is bob
    assert out[0][1]["peers"] == [
        {"username": "alice", "addr": ["127.0.0.1", 4001]}
    ]
    assert out[1][0] is alice                  # not sent to bob himself
    assert out[1][1]["username"] == "bob"
    print("PASS second login: LOGIN_OK before PEER_JOINED, self excluded")

    # --- duplicate name --------------------------------------------------
    dup = tracker.add(FakeConn(4003))
    out = tracker.handle_login(dup, "alice", True, now=1.2)
    assert kinds(out) == ["LOGIN_ERR"]
    assert out[0][1]["reason"] == "name_taken"
    print("PASS duplicate name rejected")

    # --- bad credentials / attempts --------------------------------------
    bad = tracker.add(FakeConn(4004))
    for attempt in range(1, 4):
        out = tracker.handle_login(bad, "carol", False, now=2.0)
        assert kinds(out) == ["LOGIN_ERR"]
        assert bad.attempts == attempt
    print("PASS bad credentials counted, 3 attempts")

    # --- unauthenticated traffic -----------------------------------------
    out = tracker.handle(bad, {"t": "CHAT_SEND", "text": "hi"}, now=2.1)
    assert kinds(out) == ["ERROR"]
    assert out[0][1]["code"] == "NOT_AUTHENTICATED"
    print("PASS unauthenticated chat rejected")

    # --- chat ordering + broadcast to sender -----------------------------
    out = tracker.handle(alice, {"t": "CHAT_SEND", "text": "hello"}, now=3.0)
    assert kinds(out) == ["CHAT", "CHAT"]
    assert {s.username for s, _ in out} == {"alice", "bob"}
    assert out[0][1]["gseq"] == 1
    assert out[0][1]["from"] == "alice"
    assert isinstance(out[0][1]["ts"], int)

    out = tracker.handle(bob, {"t": "CHAT_SEND", "text": "hey"}, now=3.1)
    assert out[0][1]["gseq"] == 2
    assert [entry["gseq"] for entry in tracker.history] == [1, 2]
    print("PASS chat: gseq 1 then 2, everyone including sender")

    # --- logout / PEER_LEFT ----------------------------------------------
    out = tracker.disconnected(bob, now=4.0)
    assert kinds(out) == ["PEER_LEFT"]
    assert out[0][0] is alice
    assert out[0][1]["username"] == "bob"
    assert tracker.disconnected(bob, now=4.0) == []   # idempotent
    print("PASS logout: PEER_LEFT once, idempotent")

    # --- late join gets history and current roster -----------------------
    carol = tracker.add(FakeConn(4005))
    out = tracker.handle_login(carol, "carol", True, now=5.0)
    assert kinds(out) == ["LOGIN_OK", "PEER_JOINED"]
    assert out[0][1]["peers"] == [
        {"username": "alice", "addr": ["127.0.0.1", 4001]}
    ]
    assert [entry["gseq"] for entry in out[0][1]["history"]] == [1, 2]
    print("PASS late join: roster + history replayed")

    print("\nall tracker state-machine checks passed")


if __name__ == "__main__":
    main()
