# Gateway API (HTTP + SSE) — for the web UI

The browser talks only to `client/gateway.py`, never to the transport. The
gateway is local: it binds `127.0.0.1` and has no token. Do not expose it on a
public interface.

Start the app:

```bash
# tracker
python3 -m app.tracker_server --port 6767

# client daemon + gateway (no credentials: log in through the page)
python3 -m client.daemon --tracker 127.0.0.1:6767

# open http://127.0.0.1:8080/
```

`--gateway-port` changes the port (default 8080). If you start with
`--username`, the daemon is already logged in and `POST /api/login` returns
409; always call `GET /api/state` first.

## JSON conventions

- Request bodies: JSON object, `Content-Type: application/json`.
- Responses: JSON object with `ok` boolean.
- Errors: non-2xx status plus `{"ok": false, "reason": "..."}`.
- One request per TCP connection (`Connection: close`), except SSE.

## Routes

### `GET /` and static files

Serves `web/` from the repo root. Missing files → 404 JSON.

### `GET /api/state`

```json
{
  "ok": true,
  "state": {
    "status": "idle",
    "username": null,
    "tracker": ["127.0.0.1", 6767],
    "peers": {},
    "history": []
  }
}
```

`status`: `idle`, `connecting`, `authenticated`, `disconnected`, `error`.

`peers` is keyed by username:

```json
{
  "bob": {
    "username": "bob",
    "addr": ["192.168.1.20", 51000]
  }
}
```

### `POST /api/login`

Request:

```json
{"username": "alice", "password": "secret1"}
```

Success `200`:

```json
{"ok": true, "state": { "...": "same shape as /api/state" }}
```

Failure `401`:

```json
{"ok": false, "reason": "bad_credentials"}
```

Possible reasons: `bad_credentials`, `name_taken`, `bad_username`,
`tracker_unreachable`, `login_timeout`, `bad_reply`, `unexpected_reply`.

`409 already_logged_in`, `400 bad_request`.

The username is lowercased by the daemon; the password is sent through the
transport (Level 1 encryption will protect it later).

### `POST /api/chat`

Request:

```json
{"text": "hello"}
```

Success `200`: `{"ok": true}`.
Errors: `401 not_logged_in`, `400 empty_text` / `bad_request`.

There is **no local echo**: the message arrives back as a `chat` SSE event.

### `GET /events` (SSE)

Response headers:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

First message is always the current state:

```
data: {"kind":"state","state":{...}}

```

Then daemon events, one JSON object per `data:` line:

| `kind` | Payload |
|---|---|
| `status` | `{status, username, error?}` |
| `peers` | `{peers: {username: peer}}` |
| `history` | `{history: [{gseq, from, text, ts}]}` |
| `chat` | `{message: {t, id, gseq, from, text, ts}}` |
| `dm` | `{message: {...}}` (later) |
| `error` | `{code, text}` |

Example:

```
data: {"kind":"chat","message":{"t":"CHAT","id":"m7","gseq":1,"from":"alice","text":"hi","ts":1731234567000}}

```

A comment heartbeat is sent every 15 s:

```
: heartbeat

```

`EventSource` reconnects automatically; each reconnect starts with a fresh
`state` event. Re-render from that state and the chat history.

### `POST /api/dm`, `POST /api/file`

Reserved; currently `501 {"ok":false,"reason":"not_implemented"}`.

## Minimal page flow

```js
// 1. initial state
const initial = await fetch("/api/state").then(r => r.json());
if (initial.state.status !== "authenticated") showLoginForm();

// 2. log in
await fetch("/api/login", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({username, password}),
});

// 3. live events
const events = new EventSource("/events");
events.onmessage = (e) => {
  const ev = JSON.parse(e.data);
  if (ev.kind === "state") renderState(ev.state);
  if (ev.kind === "peers") renderPeers(ev.peers);
  if (ev.kind === "history") renderHistory(ev.history);
  if (ev.kind === "chat") appendChat(ev.message);
  if (ev.kind === "status") renderStatus(ev);
  if (ev.kind === "error") showError(ev);
};

// 4. send
await fetch("/api/chat", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({text}),
});
```

## Page ownership

| File | Owner |
|---|---|
| `web/index.html`, `web/app.js`, `web/style.css` | Auth (UI lead) |
| `web/login.js` | Auth |
| `web/chat.js` | Chat |
| `web/files.js` | Files |

`web/app.js` is the shell/router; chat and files expose an `init`/`render`
function from their modules and are called by the shell. Do not edit another
page's file without coordinating with the owner.

## What the UI must not do

- open UDP sockets or import Python modules
- compute packet headers, sequence numbers, or ACKs
- call `client/daemon.py` directly
- use WebSockets; only HTTP and SSE are part of the project

## Testing the gateway without the UI

```bash
python3 scripts/gateway_demo.py
```

It exercises static serving, state, login, chat, and SSE against a real
tracker and daemon.
