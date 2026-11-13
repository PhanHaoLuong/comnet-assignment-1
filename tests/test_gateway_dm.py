"""HTTP tests for POST /api/dm and the DM part of GET /api/state."""

import asyncio
import json
import unittest

from app.peer import MAX_DM_TEXT_BYTES
from app.tracker_server import TrackerServer
from client.daemon import ClientDaemon
from client.gateway import Gateway


async def dev_auth(username, password):
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


async def http_json(host, port, method, path, payload=None):
    reader, writer = await asyncio.open_connection(host, port)
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1") + body
    writer.write(request)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    head, _, data = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(data.decode("utf-8"))


class TestGatewayDM(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = TrackerServer(dev_auth, host="127.0.0.1", port=0)
        await self.server.start()
        self.a = ClientDaemon(self.server.address, listen_host="127.0.0.1")
        self.b = ClientDaemon(self.server.address, listen_host="127.0.0.1")
        await self.a.login("alice", "pw")
        await self.b.login("bob", "pw")
        await until(lambda: "bob" in self.a.peers and "alice" in self.b.peers)

        self.gateway = Gateway(self.a, host="127.0.0.1", port=0)
        await self.gateway.start()
        self.host, self.port = self.gateway.address

    async def asyncTearDown(self):
        await self.gateway.stop()
        await self.a.stop()
        await self.b.stop()
        await self.server.stop()

    async def post(self, payload):
        return await http_json(self.host, self.port, "POST", "/api/dm", payload)

    async def test_dm_is_delivered_and_kept_in_state(self):
        status, body = await self.post({"to": "bob", "text": "via gateway"})
        self.assertEqual((status, body), (200, {"ok": True}))

        event = await event_of(
            self.b,
            lambda e: e.get("kind") == "dm" and e.get("direction") == "incoming",
        )
        self.assertEqual(event["message"]["from"], "alice")
        self.assertEqual(event["message"]["text"], "via gateway")

        _, state = await http_json(self.host, self.port, "GET", "/api/state")
        self.assertTrue(state["ok"])
        sent = state["state"]["dms"]["bob"]
        self.assertEqual(sent[-1]["text"], "via gateway")
        self.assertEqual(sent[-1]["direction"], "outgoing")

    async def test_dm_validation(self):
        status, body = await self.post({"to": "bob", "text": "   "})
        self.assertEqual((status, body["reason"]), (400, "empty_text"))

        status, body = await self.post({"to": "A", "text": "hi"})
        self.assertEqual((status, body["reason"]), (400, "bad_username"))

        status, body = await self.post({"to": "bob"})
        self.assertEqual((status, body["reason"]), (400, "bad_request"))

        status, body = await self.post({"to": "charlie", "text": "hi"})
        self.assertEqual((status, body["reason"]), (404, "peer_offline"))

        status, body = await self.post({
            "to": "bob", "text": "x" * (MAX_DM_TEXT_BYTES + 1),
        })
        self.assertEqual((status, body["reason"]), (400, "text_too_long"))

    async def test_dm_requires_login(self):
        idle = ClientDaemon(self.server.address, listen_host="127.0.0.1")
        await idle.start()
        gateway = Gateway(idle, host="127.0.0.1", port=0)
        await gateway.start()
        try:
            host, port = gateway.address
            status, body = await http_json(
                host, port, "POST", "/api/dm", {"to": "bob", "text": "hi"}
            )
            self.assertEqual((status, body["reason"]), (401, "not_logged_in"))
        finally:
            await gateway.stop()
            await idle.stop()


if __name__ == "__main__":
    unittest.main()
