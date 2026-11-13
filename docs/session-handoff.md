# Session handoff (as of Fri Nov 13 2026)

Read this first, then the four interface docs. This file captures context that
was decided in chat and is not fully written down elsewhere.

## Read in this order

1. `docs/team-handoff.md` — ownership, file map, shared contracts, collision rules
2. `docs/transport-api.md` — what app code may call
3. `docs/auth-tracker-boundary.md` — auth ↔ tracker seam
4. `docs/gateway-api.md` — HTTP/SSE contract for the web UI
5. this file — current state, recent fixes, gotchas

## Project snapshot

- Course assignment: distributed, streaming-based client-server collaboration
  app over UDP, Python, raw sockets, hand-written HTTP/UI.
- Plan for **Sun Nov 15, 2026**; assignment text says Nov 17 (a Tuesday), so
  Nov 17 is the fallback.
- Team of 5, split by feature:
  - **Lương**: transport + tracker + client daemon/gateway integration
  - **Auth**: `app/auth.py` + web UI lead (shell, login page)
  - **Crypto**: `transport/crypto.py` + integration with Lương
  - **Chat**: chat UI + chat events/methods
  - **Files**: `app/files.py`, `app/peer.py`, file UI
- The two PDFs in the repo root are the assignment and the team spec.

## Current state

| Area | State |
|---|---|
| `common/` (packet, net, constants, simulator) | done |
| `transport/connection.py`, `transport/stream.py` | done |
| `app/messages.py` | done |
| `app/tracker.py`, `app/tracker_server.py` | done |
| `app/auth.py`, `app/admin.py` | done, PBKDF2 |
| `client/daemon.py` | done: login with retry, peers, chat, DMs, state |
| `client/gateway.py` | done: HTTP/1.1 + SSE + `/api/dm` |
| `web/` | working skeleton: login, presence, DM; Auth owns the final shell |
| `app/peer.py` | done: direct P2P DM sessions + bounded in-memory log |
| `app/files.py` | not started |
| DM client peer sessions | done core; no persistence/acks/multi-device |
| `transport/crypto.py` | done Level 1: wired, tested |

Verified at handoff:

```bash
python3 -m py_compile common/*.py transport/*.py app/*.py client/*.py
python3 scripts/discovery_demo.py     # tracker end to end
python3 scripts/gateway_demo.py       # HTTP + SSE
```

## Important context not obvious from the docs

### Environment

- This machine runs **Python 3.9.6 with LibreSSL 2.8.3**; `hashlib.scrypt` does
  not exist. Auth was switched to **PBKDF2-HMAC-SHA256, 600,000 iterations**.
- `cryptography` is **not installed**. The crypto owner must install it
  (`python3 -m pip install cryptography`) for X25519/AES-GCM.
- Python 3.9: no `match`, no `X | Y` type unions without
  `from __future__ import annotations`, no `asyncio.timeout`.

### Auth

- `app/auth.py` interface: `async def authenticate(username, password) -> bool`.
- Record format: `{username, algorithm: "pbkdf2_sha256", iterations, salt,
  password_hash}`. No scrypt fields remain.
- `data/accounts.json` exists with `alice` and `bob` (PBKDF2; passwords were
  created by the auth owner and are not known to the transport side).
  Create accounts with:
  ```bash
  python3 -m app.admin add alice --password secret1
  python3 -m app.admin list
  ```
- `--dev-auth` on the tracker accepts any user with password `pw`; remove it
  from real run instructions once the demo is done.
- Auth attempts are counted **per transport connection**. After 3 failures the
  tracker closes; a later login reconnects and resets the count.

### Client login retry (recent fix)

- A failed `LOGIN` no longer closes the tracker connection. `ClientDaemon.login`
  keeps it and reuses it on retry, so the tracker's 3-attempt count applies.
- The daemon only reconnects after a real close/timeout, or when the previous
  connection is not `ESTABLISHED`.
- Gateway `POST /api/login` can be called again after `401`; the login form
  should stay visible.
- CLI with `--username` now **exits** on a failed login and prints
  `start without --username to log in through the web UI`. It no longer waits
  forever.
- Without `--username`, the client starts the gateway and waits for the page to
  log in.

### Client/gateway

- `python3 -m client.daemon --tracker HOST:PORT [--username USER]`
  - starts the daemon + gateway on `127.0.0.1:8080` by default
  - `--gateway-port` changes it, `--no-gateway` runs daemon only
  - `--password` is prompted if omitted
- Gateway is **local only** (`127.0.0.1`), no token. Do not expose it.
- `GET /events` sends an initial `{"kind":"state",...}` event, then daemon
  events; heartbeat comment every 15 s.
- `daemon.events` is a **single-consumer queue** owned by the gateway; the
  gateway fans out to SSE clients. Do not add a second consumer.
- `POST /api/dm` and `/api/file` return `501` until implemented.
- `web/index.html` is a placeholder. Auth owns `web/index.html`,
  `web/app.js`, `web/style.css`, `web/login.js`; Chat owns `web/chat.js`;
  Files owns `web/files.js`.
- Inbound peer connections are currently rejected cleanly in
  `ClientDaemon._on_peer_connection` until DM/files are implemented.

### Transport facts worth knowing

- 19-byte header; `pkt_num` changes on retransmit, `seq` does not.
- Streams: fragmentation/EOM, in-order delivery, SACK, RTO, fast retransmit
  (3 dup ACKs **or** SACK evidence of 3+ later packets), fixed 32-packet window.
- Flow control uses the receiver's sequence-range window
  (`next_seq < cum_ack + peer_window`) plus a semaphore. This fixed a real
  overrun bug found under 20% loss.
- PING every 5 s; peer dead after 15 s; `on_close` fires on FIN or silence.
- No congestion control (AIMD), no relay/`INTRO`, no Level 2 identity keys.
- `common/simulator.py` (`LossyLink`) is dev/test tooling but currently lives
  in `common/`. Move it to `tests/` or `scripts/` during cleanup.
- `tests/fakes.py` is test infrastructure; production must not import it.
- Root `packet.py`, `peer.py`, `tracker.py`, `account.txt` are older
  experiments; ignore them and do not import them.

### Repo observations

- `scripts/client_daemon_demo.py` was deleted; do not reference it.
- `scripts/login_tests.py` belongs to the auth owner; do not edit.
- `data/` is untracked. Decide whether the demo accounts file belongs in the
  submission; document account creation in the README either way.
- The root PDFs and some scripts/tests are untracked in git.

## Next work

1. **Auth/UI**: replace the skeleton `web/index.html`, `app.js`, `style.css`,
   `login.js` with the real shell; keep the form visible after a `401`. The DM
   module (`web/dm.js`) already exposes `init/setPeers/setState/onEvent/open`.
2. **Chat**: `web/chat.js`; consume `chat`/`history` SSE events; no local echo.
3. **Files**: `app/files.py` chunk/hash/temp+rename; file UI; extend
   `app/peer.py` dispatch for `FILE_*`; needs `ClientDaemon.send_file`.
4. **DM polish**: `web/dm.js` (DM owner), delivery semantics, Level 2 identity;
   persistence/offline/multi-device sync are explicitly future work.
5. **Lương**: 10–20 client stress test, README/report, review crypto and
   client changes, keep transport/tracker API frozen.

## Known non-goals for this milestone

- AIMD/congestion control
- relay/`INTRO` fallback
- Level 2 signed identities
- account-creation UI
- reconnect/backoff after tracker loss
- multi-device sessions

Mention them in the report's future work.
