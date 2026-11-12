# Auth ↔ Tracker boundary (parallel work)

Two people, two files, one interface. Neither file imports the other's internals.

| File | Owner | Responsibility |
|---|---|---|
| `app/auth.py` | auth owner | credentials, hashing, user storage |
| `app/tracker.py` | tracker owner | roster, presence, `gseq`, history, routing |
| `app/messages.py` | tracker owner | JSON encode/decode/validate for all app messages |
| `transport/connection.py` | transport owner | already done; do not modify |

## The only shared function

```python
# app/auth.py
async def authenticate(username: str, password: str) -> bool:
    """Return True only if the credentials are valid.

    username is already normalized by the caller:
      3-20 chars, lowercase [a-z0-9_].
    password is the raw UTF-8 string.
    Bad credentials return False (never raise for them).
    Raise only for an unrecoverable storage problem.
    Never log the password.
    """
```

What auth must **not** do:

- send or receive transport messages
- know about `Connection`, `Stream`, JSON, or `gseq`
- enforce username uniqueness (the roster does that)
- count login attempts (the tracker does that)

What auth **should** do:

- store salted PBKDF2-HMAC-SHA256 hashes (600k iterations), never plaintext
- run the CPU-heavy hash in `asyncio.to_thread` / `run_in_executor` so the
  event loop is not blocked
- use the same generic failure for unknown user and wrong password

## The tracker side

```python
class Session:
    conn: Connection
    username: str | None
    authenticated: bool
    attempts: int
    last_seen: float

class Tracker:
    def add(self, conn) -> Session
    def handle_login(self, session, username, ok, now) -> list[tuple[Session, dict]]
    def handle(self, session, msg, now) -> list[tuple[Session, dict]]
    def disconnected(self, session, now) -> list[tuple[Session, dict]]
```

`handle_login` is **sync** and takes the auth result as a boolean. That keeps
the state machine testable without async or sockets.

## LOGIN flow in the daemon (the only async part)

```python
raw = await session.conn.control_stream.recv()
msg = messages.decode(raw)

if msg["t"] == "LOGIN":
    username = msg["username"].lower()
    if not messages.is_valid_username(username):
        outgoing = [ (session, {"t": "LOGIN_ERR", "reason": "bad_username"}) ]
    else:
        ok = await authenticate(username, msg["password"])
        outgoing = tracker.handle_login(session, username, ok, now())
else:
    outgoing = tracker.handle(session, msg, now())

for target, reply in outgoing:
    await target.conn.control_stream.send(messages.encode(reply))
```

## Rules the tracker owns

- **Order:** `LOGIN_OK` to the new session first, then `PEER_JOINED` to others.
  The new user is excluded from their own `peers[]` and from the broadcast.
- **Duplicate name:** checked *after* successful auth. Same name, different
  session → `LOGIN_ERR {reason: "name_taken"}`. Not a credential failure.
- **Attempts:** count only credential failures; after 3, send the error and
  close the connection.
- **Unauthenticated traffic:** anything except `LOGIN` → `ERROR {code: NOT_AUTHENTICATED}`.
- **`LOGIN_OK`:** `peers` (roster snapshot), `history` (last N chats),
  `your_addr = list(session.conn.peer_addr)`.
- **`disconnected()` is idempotent:** it can be reached from `LOGOUT`,
  `on_close`, and the sweeper.

## Stub until auth is ready

```python
async def fake_auth(username, password):
    return password == "pw"

tracker = Tracker(authenticate=fake_auth)
```

Swap the function later; tracker code does not change.

## Test split

- Auth owner: valid/invalid password, unknown user, hash format, timing.
- Tracker owner: everything in the state machine with a stubbed auth:
  order, self-exclusion, duplicate names, attempts, chat `gseq`, history,
  `PEER_LEFT`, late join.
- No socket is needed for either suite.
