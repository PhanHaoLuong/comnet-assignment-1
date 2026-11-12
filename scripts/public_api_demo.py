"""Minimal end-to-end example using ONLY the public transport API.

Run from the repo root:

    python3 scripts/public_api_demo.py

What this shows a teammate:
  * ConnectionManager.listen / address / on_connection
  * ConnectionManager.connect
  * conn.control_stream.send / recv
  * conn.on_close, and why callbacks must spawn a task
  * JSON messages above the transport (LOGIN / LOGIN_OK / PEER_JOINED / PEER_LEFT)

The transport never sees "LOGIN"; it only sees bytes on stream 0.
Replace the TODO auth check with the real one.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transport.connection import ConnectionManager


def enc(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def dec(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))


class DemoTracker:
    def __init__(self):
        self.mgr = ConnectionManager(on_connection=self.on_connection)
        self.by_conn = {}          # conn -> username

    async def start(self):
        await self.mgr.listen("127.0.0.1", 0)

    # on_connection is sync: spawn the async loop, never block here
    def on_connection(self, conn):
        print(f"[tracker] inbound from {conn.peer_addr} ({conn.peer_name})")
        conn.on_close(self.on_close)
        asyncio.create_task(self.serve(conn))

    async def serve(self, conn):
        try:
            while True:
                raw = await conn.control_stream.recv()
                msg = dec(raw)

                if msg["t"] == "LOGIN":
                    username = msg["username"]
                    # TODO: replace with the real credential check
                    if username in self.by_conn.values():
                        await conn.control_stream.send(
                            enc({"t": "LOGIN_ERR", "reason": "NAME_TAKEN"})
                        )
                        continue

                    self.by_conn[conn] = username
                    peers = [
                        {"username": other_name,
                         "addr": list(other.peer_addr)}
                        for other, other_name in self.by_conn.items()
                        if other is not conn
                    ]
                    await conn.control_stream.send(
                        enc({"t": "LOGIN_OK",
                             "peers": peers,
                             "your_addr": list(conn.peer_addr)})
                    )
                    await self.broadcast(
                        {"t": "PEER_JOINED",
                         "username": username,
                         "addr": list(conn.peer_addr)},
                        skip=conn,
                    )
                    print(f"[tracker] {username} joined; roster={list(self.by_conn.values())}")
        except ConnectionError:
            pass

    async def broadcast(self, msg, skip=None):
        for other in list(self.by_conn):
            if other is skip:
                continue
            try:
                await other.control_stream.send(enc(msg))
            except ConnectionError:
                pass

    def on_close(self, conn):
        # sync callback: schedule the announce work
        asyncio.create_task(self.announce_left(conn))

    async def announce_left(self, conn):
        username = self.by_conn.pop(conn, None)
        if username is not None:
            await self.broadcast({"t": "PEER_LEFT", "username": username})
            print(f"[tracker] {username} left; roster={list(self.by_conn.values())}")


async def login(tracker_addr, username):
    mgr = ConnectionManager()
    await mgr.listen("127.0.0.1", 0)
    conn = await mgr.connect(tracker_addr, username)

    await conn.control_stream.send(
        enc({"t": "LOGIN", "username": username, "password": "pw"})
    )
    reply = dec(await conn.control_stream.recv())
    print(f"[{username}] {reply['t']}")
    return mgr, conn, reply


async def main():
    tracker = DemoTracker()
    await tracker.start()
    print("tracker at", tracker.mgr.address)

    alice_mgr, alice, alice_ok = await login(tracker.mgr.address, "alice")
    assert alice_ok["t"] == "LOGIN_OK"
    assert alice_ok["peers"] == []

    bob_mgr, bob, bob_ok = await login(tracker.mgr.address, "bob")
    assert bob_ok["t"] == "LOGIN_OK"
    assert [p["username"] for p in bob_ok["peers"]] == ["alice"]

    # alice receives the join announcement on the same control stream
    joined = dec(await asyncio.wait_for(alice.control_stream.recv(), timeout=2))
    print(f"[alice] {joined['t']} {joined['username']}")
    assert joined == {
        "t": "PEER_JOINED",
        "username": "bob",
        "addr": list(bob_mgr.address),
    }

    # graceful leave: tracker gets FIN -> PEER_LEFT
    bob.close()
    left = dec(await asyncio.wait_for(alice.control_stream.recv(), timeout=2))
    print(f"[alice] {left['t']} {left['username']}")
    assert left == {"t": "PEER_LEFT", "username": "bob"}

    alice_mgr.endpoint.close()
    bob_mgr.endpoint.close()
    tracker.mgr.endpoint.close()
    print("demo complete")


if __name__ == "__main__":
    asyncio.run(main())
