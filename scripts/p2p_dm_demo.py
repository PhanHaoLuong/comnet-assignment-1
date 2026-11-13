"""Real tracker + two ClientDaemons + direct peer-to-peer UDP messages.

Run from repo root: python scripts/p2p_dm_demo.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tracker_server import TrackerServer
from client.daemon import ClientDaemon


async def auth(username: str, password: str) -> bool:
    return password == "pw"


async def until(predicate, timeout: float = 3.0):
    async def loop():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(loop(), timeout)


async def await_dm(daemon: ClientDaemon, sender: str, content: str, timeout: float = 3.0):
    async def loop():
        while True:
            item = await daemon.events.get()
            if (item.get("kind") == "dm"
                    and item.get("direction") == "incoming"
                    and item["message"]["from"] == sender
                    and item["message"]["text"] == content):
                return item
    return await asyncio.wait_for(loop(), timeout)


async def main():
    tracker = TrackerServer(auth, host="127.0.0.1", port=0)
    await tracker.start()
    alice = ClientDaemon(tracker.address, listen_host="127.0.0.1")
    bob = ClientDaemon(tracker.address, listen_host="127.0.0.1")
    try:
        await alice.login("alice", "pw")
        await bob.login("bob", "pw")
        await until(lambda: "bob" in alice.peers and "alice" in bob.peers)
        print("PASS tracker discovery: Alice and Bob know each other's UDP address")
        print("alice UDP:", alice.address, " bob UDP:", bob.address)

        await alice.send_dm("bob", "hello bob, direct UDP")
        got = await await_dm(bob, "alice", "hello bob, direct UDP")
        assert got["message"]["to"] == "bob"
        print("PASS direct DM: Alice -> Bob")

        await bob.send_dm("alice", "hi alice, no tracker relay")
        await await_dm(alice, "bob", "hi alice, no tracker relay")
        print("PASS direct DM: Bob -> Alice")

        for index in range(15):
            await alice.send_dm("bob", f"ordered-{index:02d}")
        for index in range(15):
            await await_dm(bob, "alice", f"ordered-{index:02d}")
        print("PASS 15 messages delivered in order")

        assert len(tracker.tracker.history) == 0
        print("PASS tracker CHAT history unchanged (DM did not travel through tracker)")

        try:
            await alice.send_dm("charlie", "not online")
        except RuntimeError:
            print("PASS offline peer rejected")
        else:
            raise AssertionError("expected offline peer to fail")

        print("\nALL P2P DM CHECKS PASSED")
    finally:
        await alice.stop()
        await bob.stop()
        await tracker.stop()


if __name__ == "__main__":
    asyncio.run(main())
