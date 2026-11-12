# Team handoff: ownership & interfaces

This file is the contract for parallel work. If two people need to change the
same file, the owner of that file does the merge. Do not edit another owner's
file without agreement.

## Current state

| Area | State |
|---|---|
| `common/` (packet, net, constants) | done |
| `transport/connection.py`, `transport/stream.py` | done: handshake, streams, SACK, RTO, fast retransmit, flow control, PING/dead detection |
| `app/messages.py` | done: JSON schema + validation |
| `app/tracker.py` | done: roster, presence, `gseq`, history |
| `app/tracker_server.py` | done: tracker daemon |
| `app/auth.py` | done: PBKDF2-HMAC-SHA256 |
| `client/daemon.py` | done: login, peers, chat events, state, stop |
| `client/gateway.py` | done: HTTP/1.1 + SSE bridge |
| `web/` | placeholder `index.html`; Auth owns the real shell + login page |
| `app/files.py`, `app/peer.py` | **not started** |
| `transport/crypto.py` | **not started** |

Useful references:

- `docs/transport-api.md` — the transport surface for app code
- `docs/auth-tracker-boundary.md` — auth ↔ tracker contract
- `docs/gateway-api.md` — HTTP/SSE contract for the web UI
- `scripts/discovery_demo.py` — end-to-end tracker test
- `scripts/gateway_demo.py` — HTTP/SSE gateway test
- `scripts/public_api_demo.py` — smallest client/tracker example

## Ownership map

One owner per file. "Integration" means the person who merges changes into
shared client code.

| Owner | Files |
|---|---|
| **Lương** (transport + tracker) | `common/**`, `transport/connection.py`, `transport/stream.py`, `app/messages.py`, `app/tracker.py`, `app/tracker_server.py`, `docs/**` |
| **Lương** (client integration) | `client/daemon.py`, `client/gateway.py` |
| **Auth** (auth + web UI lead) | `app/auth.py`, `web/index.html`, `web/app.js`, `web/style.css`, `web/login.js` |
| **Crypto** | `transport/crypto.py` (integration into `connection.py` is reviewed by Lương) |
| **Chat** | `web/chat.js`, chat-related methods/events in `client/daemon.py` (via integration owner), `app/chat.py` if needed |
| **Files** | `app/files.py`, `app/peer.py`, `web/files.js` |

If `client/daemon.py` + `client/gateway.py` are reassigned, move the whole
block to one person. Never split that file pair.

The Auth owner now owns the web shell and login page. Chat and Files add
separate JS modules (`web/chat.js`, `web/files.js`) and coordinate any
`web/app.js` change with Auth. `client/gateway.py` changes still go through
Lương.

## Shared contracts

### 1. Transport (everyone)

```python
from transport.connection import ConnectionManager

mgr = ConnectionManager(on_connection=...)
await mgr.listen(host, port)         # every peer listens
conn = await mgr.connect(addr, username)
mgr.address

conn.control_stream.send(bytes)      # reliable, ordered, one message per call
msg = await conn.control_stream.recv()

stream = conn.open_stream()          # 2,4,... or 3,5,...
conn.on_stream(cb)                   # inbound stream
conn.on_close(cb)                    # FIN or silent death
conn.peer_addr / conn.peer_name
```

- callbacks are **sync**; spawn a task for `await` work
- `recv()` raises `ConnectionError` after close
- PING runs automatically; dead after 15 s of silence
- do not import `common.packet`, `transport.stream`, `tests/`, `scripts/`

### 2. Messages (`app/messages.py`)

Import as `from app import messages`.

```python
raw = messages.encode(msg)           # validate + UTF-8 JSON
msg = messages.decode(raw)           # parse + validate
msg = messages.make("CHAT_SEND", text="hi")   # adds id
messages.is_valid_username(value)
```

Envelope: `t`, `id` (required), `reply_to` (optional).
Timestamps: Unix epoch **milliseconds**, int.
Addresses: `[ip, port]`, port 0–65535.
Usernames: 3–20 chars, **lowercase** `[a-z0-9_]`.
Unknown extra fields are tolerated; missing/wrong fields raise `MessageError`.

| Type | Direction | Required fields |
|---|---|---|
| `LOGIN` | C→T | `username`, `password` |
| `LOGIN_OK` | T→C | `peers`, `history`, `your_addr` |
| `LOGIN_ERR` | T→C | `reason` |
| `PEER_JOINED` | T→C | `username`, `addr` |
| `PEER_LEFT` | T→C | `username` |
| `CHAT_SEND` | C→T | `text` |
| `CHAT` | T→C | `gseq`, `from`, `text`, `ts` |
| `CONNECT_REQ` | C→T | `target` |
| `INTRO` | T→C | `username`, `addr` |
| `RELAY` | C/S→ | `to`, `payload` |
| `LOGOUT` | C→T | — |
| `ERROR` | T→C | `code`, `text` |
| `DM` | P→P | `text`, `ts` |
| `FILE_OFFER` | P→P | `transfer_id`, `name`, `size`, `sha256`, `stream_id` |
| `FILE_ACCEPT` | P→P | `transfer_id` |
| `FILE_REJECT` | P→P | `transfer_id`, `reason` |
| `FILE_END` | P→P | `transfer_id`, `total_bytes`, `sha256` |
| `FILE_DONE` | P→P | `transfer_id`, `ok` (`reason` optional) |
| `FILE_ABORT` | P→P | `transfer_id`, `reason` |

`peers[]` entry: `{username, addr, identity_pub?}`.
`history[]` entry: `{gseq, from, text, ts}`.
`ERROR.code`: `BAD_CREDENTIALS`, `NAME_TAKEN`, `NOT_AUTHENTICATED`,
`UNKNOWN_PEER`, `BAD_MESSAGE`.

### 3. Auth seam

```python
# app/auth.py  (Auth owner)
async def authenticate(username: str, password: str) -> bool:
    ...
```

- username already lowercase/validated
- return `False` for bad credentials; raise only on storage failure
- never log passwords; hash with PBKDF2-HMAC-SHA256 (600k iterations); run CPU work off the loop
- auth does not send messages, know JSON, or enforce uniqueness

The tracker calls it from `app/tracker_server.py`. Until it exists, run the
tracker with `--dev-auth` (password `pw`).

### 4. Client daemon ↔ gateway

One `ClientDaemon` per user process. Gateway/UI must use only this surface:

```python
daemon.events            # asyncio.Queue[dict], single consumer (gateway)
daemon.state() -> dict   # {status, username, peers, history}
await daemon.send_chat(text)
await daemon.send_dm(to, text)          # later
await daemon.send_file(to, path)        # later
```

Event kinds:

| kind | payload |
|---|---|
| `status` | `{connected, username, tracker}` |
| `peers` | `{peers: {username: info}}` |
| `history` | `{history: [chat entries]}` |
| `chat` | `{message: CHAT dict}` |
| `dm` | `{message: DM dict}` |
| `file_progress` | `{transfer_id, received, size, direction}` |
| `error` | `{code, text}` |

Rules:

- only integration owner edits the tracker read loop
- chat/files owners add methods/events through the integration owner
- gateway consumes `events` and exposes SSE
- no HTTP code inside `client/daemon.py`

### 5. Crypto seam

Crypto owner writes `transport/crypto.py`; integration owner wires it.

```python
class CryptoContext:
    def encrypt(self, pkt_num: int, header: bytes, payload: bytes) -> bytes: ...
    def decrypt(self, pkt_num: int, header: bytes, payload: bytes) -> bytes: ...
```

Level 1 rules:

- X25519 ephemeral per connection, HKDF-SHA256, AES-256-GCM
- nonce from role byte + `pkt_num` (never from `seq`)
- 19-byte header is AAD; set `ENC` before building the AAD
- HELLO/HELLO_ACK `eph_pub`/`nonce` are zeros now; crypto fills them
- replay window on `pkt_num`
- do not change the public transport API

Integration points in `connection.py`: HELLO/HELLO_ACK construction, `_transmit`,
and `on_packet` before dispatch. Coordinate every change with Lương.

## Per-owner scope and done criteria

### Auth (auth + web UI lead)

- `app/auth.py`: done (PBKDF2, hashed store, admin tool)
- web shell: `web/index.html`, `web/app.js`, `web/style.css`
- login page: `web/login.js`, uses `POST /api/login` and `GET /api/state`
- read `docs/gateway-api.md` before touching the UI
- chat/files add their own modules; coordinate any `web/app.js` change with Auth

### Crypto

- `transport/crypto.py` + unit tests with fixed vectors
- handshake key derivation and nonce rules documented
- integration PR into `connection.py` reviewed by Lương
- test: encrypt/decrypt round-trip, tampered ciphertext rejected, replay rejected

### Chat

- UI: send box, message list, sender names, history on login
- never locally echo: render the `CHAT` received from the tracker
- order by `gseq`; ignore duplicates on reconnect
- `client/daemon.py` additions go through integration owner

### Files

- `app/files.py`: chunking, sha256, temp file + rename, progress
- `app/peer.py`: peer session helper if needed
- flow: `FILE_OFFER` (stream 0) → `FILE_ACCEPT` → raw chunks on a new stream →
  `FILE_END` → verify hash → `FILE_DONE`
- send chunks as separate `Stream.send` messages (~64 KB), never one giant send
- UI: file picker, progress, accept/reject

### Integration/UI (Lương)

- `client/daemon.py`: done (login, `peers`, chat events, `state()`, `send_chat`)
- `client/gateway.py`: done (`GET /`, `GET /events`, `POST /api/login`,
  `POST /api/chat`, `GET /api/state`)
- `web/`: hand off to Auth (UI lead); placeholder page only
- multi-client stress test (10–20)
- gateway changes (new routes/events) stay with Lương

### Transport/tracker (Lương)

- already done; keep the API frozen
- 10–20 client stress test, README, report
- review crypto integration and client daemon changes

## Collision rules

1. One file, one owner. No drive-by edits or reformatting.
2. Shared files: `app/messages.py`, `app/tracker*.py`, `transport/**`,
   `client/daemon.py`, `client/gateway.py`, `web/app.js` — owner merges.
3. Contract change = update this doc first, then tell the owner.
4. Tests/scripts are per-owner (`tests/test_<area>.py`, `scripts/<area>_*.py`);
   production must never import them.
5. Use feature branches and small PRs. Don't mix refactors with features.
6. `--dev-auth` is development only; never ship it as the default.

## Integration order

1. `app/auth.py` → done; tracker uses it.
2. `client/daemon.py` baseline → done; unblocks chat/files/UI.
3. `client/gateway.py` + HTTP/SSE → done; web pages are next.
4. `app/files.py` + file UI → depends on daemon and streams.
5. `transport/crypto.py` → integration after the rest works, so failures are
   isolated. Crypto is still required for the security rubric point.
6. DM (client peer sessions) after daemon and files primitives.

## How to run what exists

```bash
# tracker (development auth)
python3 -m app.tracker_server --dev-auth --port 6767

# end-to-end discovery: login, roster, chat, PEER_LEFT, silent death
python3 scripts/discovery_demo.py

# transport checks
python3 scripts/stream_udp_demo.py --bytes 100000
python3 scripts/stream_udp_demo.py --bytes 50000 --loss 0.2 --seed 3
python3 scripts/multistream_demo.py --bytes 500000
python3 scripts/ping_pong_demo.py

# client daemon + gateway
python3 scripts/gateway_demo.py
```

Client process (daemon + gateway):

```bash
python3 -m client.daemon --tracker 127.0.0.1:6767 --username alice
# opens the HTTP/SSE gateway on http://127.0.0.1:8080/
# omit --username to log in through the web UI instead
# --no-gateway runs the daemon alone and prints events
```

## Known non-goals for this milestone

- congestion control (AIMD)
- relay/`INTRO` fallback when direct P2P fails
- Level 2 signed identity keys (`identity_pub` is currently absent)
- account creation UI; auth decides auto-register vs seeded users
- reconnection/backoff after tracker loss
- multi-device sessions

Document any of these that you do not finish in the report's "future work".
