"""UDP P2P application tests (no E2EE). Run: python -m unittest discover -s tests -v"""

import asyncio
import unittest

from app import messages
from app.peer import MAX_DM_TEXT_BYTES
from app.tracker_server import TrackerServer
from client.daemon import ClientDaemon
from transport.connection import ConnectionManager


async def auth(username, password):
    return password == "pw"


async def until(predicate, timeout=3):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


async def event_of(daemon, predicate, timeout=3):
    async def wait():
        while True:
            event = await daemon.events.get()
            if predicate(event):
                return event
    return await asyncio.wait_for(wait(), timeout)


class TestDirectPeerMessaging(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = TrackerServer(auth, host="127.0.0.1", port=0)
        await self.server.start()
        self.a = ClientDaemon(self.server.address, listen_host="127.0.0.1")
        self.b = ClientDaemon(self.server.address, listen_host="127.0.0.1")
        await self.a.login("alice", "pw")
        await self.b.login("bob", "pw")
        await until(lambda: "bob" in self.a.peers and "alice" in self.b.peers)

    async def asyncTearDown(self):
        await self.a.stop()
        await self.b.stop()
        await self.server.stop()

    async def test_bidirectional_dm(self):
        await self.a.send_dm("bob", "hello")
        ev = await event_of(self.b, lambda e: e.get("kind") == "dm" and
                            e.get("direction") == "incoming")
        self.assertEqual((ev["message"]["from"], ev["message"]["text"]), ("alice", "hello"))
        await self.b.send_dm("alice", "response")
        ev = await event_of(self.a, lambda e: e.get("kind") == "dm" and
                            e.get("direction") == "incoming")
        self.assertEqual((ev["message"]["from"], ev["message"]["text"]), ("bob", "response"))
        self.assertEqual(len(self.server.tracker.history), 0)

    async def test_reuses_session_and_ordered_messages(self):
        await self.a.send_dm("bob", "first")
        await event_of(self.b, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")
        conns = list(self.a.peer_service._sessions["bob"])
        for n in range(10):
            await self.a.send_dm("bob", f"m{n}")
        got = []
        while len(got) < 10:
            e = await event_of(self.b, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")
            got.append(e["message"]["text"])
        self.assertEqual(got, [f"m{i}" for i in range(10)])
        self.assertEqual(self.a.peer_service._sessions["bob"], conns)

    async def test_offline_user_and_empty_message(self):
        with self.assertRaises(RuntimeError):
            await self.a.send_dm("charlie", "offline")
        with self.assertRaises(ValueError):
            await self.a.send_dm("bob", "")

    async def test_forged_from_field_is_not_trusted(self):
        await self.a.send_dm("bob", "warm-up")
        await event_of(self.b, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")
        conn = self.a.peer_service._sessions["bob"][0]
        forged = messages.make("DM", text="spoof", ts=123, **{"from": "mallory"})
        await conn.control_stream.send(messages.encode(forged))
        e = await event_of(self.b, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")
        self.assertEqual(e["message"]["from"], "alice")

    async def test_unregistered_peer_cannot_enter_application_session(self):
        outsider = ConnectionManager()
        await outsider.listen("127.0.0.1", 0)
        try:
            c = await outsider.connect(self.a.address, "mallory")
            await until(lambda: c.state.name == "CLOSED", timeout=4)
            self.assertNotIn("mallory", self.a.peer_service.connected_peers)
        finally:
            outsider.endpoint.close()

    async def test_concurrent_first_messages(self):
        await asyncio.gather(self.a.send_dm("bob", "concurrent a"),
                             self.b.send_dm("alice", "concurrent b"))
        a_msg = await event_of(self.a, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")
        b_msg = await event_of(self.b, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")
        self.assertEqual(a_msg["message"]["text"], "concurrent b")
        self.assertEqual(b_msg["message"]["text"], "concurrent a")

    async def test_peer_left_purges_session(self):
        await self.a.send_dm("bob", "before logout")
        await event_of(self.b, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")
        await self.b.stop()
        await until(lambda: "bob" not in self.a.peers)
        await until(lambda: "bob" not in self.a.peer_service.connected_peers)

    async def test_state_contains_both_directions(self):
        await self.a.send_dm("bob", "state me")
        await event_of(self.b, lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming")

        sent = self.a.state()["dms"]["bob"]
        received = self.b.state()["dms"]["alice"]
        self.assertEqual(sent[-1]["text"], "state me")
        self.assertEqual(sent[-1]["direction"], "outgoing")
        self.assertEqual(received[-1]["text"], "state me")
        self.assertEqual(received[-1]["direction"], "incoming")
        self.assertEqual(received[-1]["from"], "alice")

    async def test_history_is_bounded_and_deduplicated(self):
        service = self.a.peer_service
        for index in range(105):
            service._append_dm("bob", {
                "id": f"id-{index}", "text": str(index), "ts": index,
                "from": "alice", "to": "bob", "direction": "outgoing",
            })
        history = service.dm_history()["bob"]
        self.assertEqual(len(history), 100)
        self.assertEqual(history[0]["id"], "id-5")

        duplicate = dict(history[-1])
        self.assertFalse(service._append_dm("bob", duplicate))
        self.assertEqual(len(service.dm_history()["bob"]), 100)

    async def test_oversized_message_rejected(self):
        with self.assertRaises(ValueError):
            await self.a.send_dm("bob", "x" * (MAX_DM_TEXT_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
