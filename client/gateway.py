"""Local HTTP/1.1 + SSE gateway between the browser and ClientDaemon.

Hand-written on asyncio streams. No web framework, no WebSockets.

    Gateway(daemon, host="127.0.0.1", port=8080)

Routes:
    GET  /                 static web/index.html
    GET  /<file>           static file under web/
    GET  /events           text/event-stream of daemon.events
    GET  /api/state        current daemon state
    POST /api/login        {username, password}
    POST /api/chat         {text}
    POST /api/dm           reserved
    POST /api/file         reserved
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from typing import Optional

from client.daemon import ClientDaemon, LoginError

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1024 * 1024
SSE_HEARTBEAT_S = 15.0

REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
}


class Gateway:
    def __init__(
        self,
        daemon: ClientDaemon,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        web_root: Optional[Path] = None,
    ):
        self.daemon = daemon
        self.host = host
        self.port = port
        self.web_root = Path(web_root) if web_root else (
            Path(__file__).resolve().parents[1] / "web"
        )

        self._server: Optional[asyncio.AbstractServer] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._subscribers: set[asyncio.Queue] = set()
        self.address: Optional[tuple[str, int]] = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        socket = self._server.sockets[0]
        self.address = socket.getsockname()[:2]
        self._pump_task = asyncio.create_task(self._pump_events())
        logger.info("gateway listening on http://%s:%s", *self.address)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self._pump_task is not None:
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)
            self._pump_task = None

        for queue in list(self._subscribers):
            queue.put_nowait(None)
        self._subscribers.clear()

    # ------------------------------------------------------------------ events

    async def _pump_events(self) -> None:
        try:
            while True:
                event = await self.daemon.events.get()
                payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                for queue in list(self._subscribers):
                    queue.put_nowait(payload)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ HTTP

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        sse = False
        try:
            request = await self._read_request(reader)
            if request is None:
                return

            method, path, headers, body = request
            if method == "GET" and path == "/events":
                sse = True
                await self._serve_events(writer)
            else:
                await self._route(method, path, body, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("gateway request failed")
            try:
                await self._respond(writer, 500, b'{"ok":false}', "application/json")
            except Exception:
                pass
        finally:
            if not sse:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _read_request(self, reader: asyncio.StreamReader):
        try:
            blob = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None

        lines = blob.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            return None

        path = target.split("?", 1)[0]
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()

        try:
            length = int(headers.get("content-length", "0") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None

        body = await reader.readexactly(length) if length else b""
        return method, path, headers, body

    async def _route(self, method: str, path: str, body: bytes,
                     writer: asyncio.StreamWriter) -> None:
        if method == "GET" and path == "/api/state":
            await self._json(writer, 200, {"ok": True, "state": self.daemon.state()})
            return

        if method == "POST" and path == "/api/login":
            await self._login(writer, body)
            return

        if method == "POST" and path == "/api/chat":
            await self._chat(writer, body)
            return

        if method == "POST" and path in ("/api/dm", "/api/file"):
            await self._json(writer, 501, {"ok": False, "reason": "not_implemented"})
            return

        if method == "GET":
            await self._static(path, writer)
            return

        await self._json(writer, 404, {"ok": False, "reason": "not_found"})

    async def _login(self, writer, body: bytes) -> None:
        try:
            data = self._parse_json(body)
            username = data["username"]
            password = data["password"]
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            await self._json(writer, 400, {"ok": False, "reason": "bad_request"})
            return

        try:
            await self.daemon.login(username, password)
        except LoginError as exc:
            await self._json(writer, 401, {"ok": False, "reason": exc.reason})
            return
        except RuntimeError:
            await self._json(writer, 409, {"ok": False, "reason": "already_logged_in"})
            return

        await self._json(writer, 200, {"ok": True, "state": self.daemon.state()})

    async def _chat(self, writer, body: bytes) -> None:
        try:
            text = self._parse_json(body)["text"]
            if not isinstance(text, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            await self._json(writer, 400, {"ok": False, "reason": "bad_request"})
            return

        try:
            await self.daemon.send_chat(text)
        except RuntimeError:
            await self._json(writer, 401, {"ok": False, "reason": "not_logged_in"})
            return
        except ValueError:
            await self._json(writer, 400, {"ok": False, "reason": "empty_text"})
            return

        await self._json(writer, 200, {"ok": True})

    async def _static(self, path: str, writer) -> None:
        if path == "/":
            path = "/index.html"

        root = self.web_root.resolve()
        target = (root / path.lstrip("/")).resolve()
        if target != root and root not in target.parents:
            await self._json(writer, 404, {"ok": False, "reason": "not_found"})
            return
        if not target.is_file():
            await self._json(writer, 404, {"ok": False, "reason": "not_found"})
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        await self._respond(writer, 200, target.read_bytes(), content_type)

    async def _serve_events(self, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)

        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"X-Accel-Buffering: no\r\n"
            b"\r\n"
        )
        initial = json.dumps(
            {"kind": "state", "state": self.daemon.state()},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        writer.write(f"data: {initial}\n\n".encode("utf-8"))
        await writer.drain()

        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(),
                                                     timeout=SSE_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    writer.write(b": heartbeat\n\n")
                    await writer.drain()
                    continue

                if payload is None:
                    break
                writer.write(f"data: {payload}\n\n".encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.discard(queue)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _parse_json(body: bytes) -> dict:
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    async def _json(self, writer, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await self._respond(writer, status, body, "application/json")

    async def _respond(self, writer, status: int, body: bytes,
                       content_type: str) -> None:
        reason = REASONS.get(status, "Error")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()
