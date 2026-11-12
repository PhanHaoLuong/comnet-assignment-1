"""PING/PONG heartbeat checks (fast timers for the demo).

    python3 scripts/ping_pong_demo.py

Scenario 1: an idle connection stays alive because PONGs refresh last_seen.
Scenario 2: closing the peer's endpoint without FIN (silent death) makes the
            other side declare it dead and fire on_close.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from transport.connection import ConnectionManager, ConnectionState

PING = 0.05
DEAD = 0.20


async def idle_connection_stays_alive():
    inbound = []
    a = ConnectionManager(ping_interval=PING, dead_timeout=DEAD)
    b = ConnectionManager(inbound.append, ping_interval=PING, dead_timeout=DEAD)
    await a.listen("127.0.0.1", 0)
    await b.listen("127.0.0.1", 0)
    try:
        alice = await asyncio.wait_for(a.connect(b.endpoint.address, "alice"), 3)
        bob = inbound[0]

        before = alice.next_pkt_num
        await asyncio.sleep(0.6)          # > 3 intervals

        assert alice.state is ConnectionState.ESTABLISHED, alice.state
        assert bob.state is ConnectionState.ESTABLISHED, bob.state
        assert alice.next_pkt_num > before, "no PINGs were sent"
        print(f"PASS idle: alive after 0.6 s, pkt_num={alice.next_pkt_num}")
    finally:
        a.endpoint.close()
        b.endpoint.close()


async def silent_death_is_detected():
    closed = []
    inbound = []
    a = ConnectionManager(ping_interval=PING, dead_timeout=DEAD)
    b = ConnectionManager(inbound.append, ping_interval=PING, dead_timeout=DEAD)
    await a.listen("127.0.0.1", 0)
    await b.listen("127.0.0.1", 0)
    try:
        alice = await asyncio.wait_for(a.connect(b.endpoint.address, "alice"), 3)
        alice.on_close(closed.append)

        b.endpoint.close()                # no FIN: simulate kill -9
        await asyncio.sleep(0.6)

        assert alice.state is ConnectionState.CLOSED, alice.state
        assert alice in closed, "on_close did not fire"
        assert len(a.connections) == 0, a.connections
        print("PASS silent death: closed after silence, manager cleaned up")
    finally:
        a.endpoint.close()


async def main():
    await idle_connection_stays_alive()
    await silent_death_is_detected()
    print("all PING/PONG checks passed")


if __name__ == "__main__":
    asyncio.run(main())
