"""End-to-end discovery check: real tracker daemon + two/three clients.

    python3 scripts/discovery_demo.py

Fast PING timers are used so silent death is detected in under a second.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import messages
from app.tracker_server import TrackerServer
from transport.connection import ConnectionManager

FAST_PING = 0.05
FAST_DEAD = 0.20


async def fake_auth(username, password):
    return password == "pw"


async def login(server_addr, username, password="pw"):
    mgr = ConnectionManager(ping_interval=FAST_PING, dead_timeout=FAST_DEAD)
    await mgr.listen("127.0.0.1", 0)
    conn = await mgr.connect(server_addr, username)
    await conn.control_stream.send(messages.encode(
        messages.make("LOGIN", username=username, password=password)
    ))
    reply = messages.decode(await asyncio.wait_for(conn.control_stream.recv(), 2))
    return mgr, conn, reply


async def recv(conn):
    raw = await asyncio.wait_for(conn.control_stream.recv(), 2)
    return messages.decode(raw)


async def main():
    server = TrackerServer(
        fake_auth,
        host="127.0.0.1",
        port=0,
        ping_interval=FAST_PING,
        dead_timeout=FAST_DEAD,
    )
    await server.start()
    print("tracker at", server.address)

    clients = []
    try:
        # --- discovery ---------------------------------------------------
        alice_mgr, alice, ok = await login(server.address, "alice")
        clients.append(alice_mgr)
        assert ok["t"] == "LOGIN_OK" and ok["peers"] == []
        print("PASS alice LOGIN_OK, empty roster")

        bob_mgr, bob, ok = await login(server.address, "bob")
        clients.append(bob_mgr)
        assert ok["t"] == "LOGIN_OK"
        assert [p["username"] for p in ok["peers"]] == ["alice"]
        joined = await recv(alice)
        assert joined["t"] == "PEER_JOINED" and joined["username"] == "bob"
        print("PASS bob LOGIN_OK with alice; alice got PEER_JOINED")

        # --- duplicate name ----------------------------------------------
        dup_mgr, dup, err = await login(server.address, "alice")
        clients.append(dup_mgr)
        assert err["t"] == "LOGIN_ERR" and err["reason"] == "name_taken"
        dup.close()
        print("PASS duplicate name rejected")

        # --- chat ordering ------------------------------------------------
        await alice.control_stream.send(messages.encode(
            messages.make("CHAT_SEND", text="hello")
        ))
        alice_chat = await recv(alice)
        bob_chat = await recv(bob)
        assert alice_chat["t"] == bob_chat["t"] == "CHAT"
        assert alice_chat["gseq"] == bob_chat["gseq"] == 1
        assert alice_chat["from"] == "alice"
        print("PASS chat: both clients got the same gseq=1 CHAT")

        # --- graceful leave ------------------------------------------------
        bob.close()
        left = await recv(alice)
        assert left["t"] == "PEER_LEFT" and left["username"] == "bob"
        print("PASS graceful leave: PEER_LEFT")

        # --- silent death (no FIN) -----------------------------------------
        charlie_mgr, charlie, ok = await login(server.address, "charlie")
        clients.append(charlie_mgr)
        joined = await recv(alice)
        assert joined["t"] == "PEER_JOINED" and joined["username"] == "charlie"

        charlie_mgr.endpoint.close()        # kill -9 simulation
        left = await recv(alice)
        assert left["t"] == "PEER_LEFT" and left["username"] == "charlie"
        print("PASS silent death: PING timeout produced PEER_LEFT")

    finally:
        for mgr in clients:
            if mgr.endpoint is not None:
                mgr.endpoint.close()
        await server.stop()

    print("\nall discovery checks passed")


if __name__ == "__main__":
    asyncio.run(main())
