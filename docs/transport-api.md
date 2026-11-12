# Transport API (v1) — for Tracks B and C

Status: **stable for app work**, with the two caveats at the bottom.
Do not import anything under `transport/` except the names below, and never
import `tests/` or `scripts/` from production code.

## Setup

Tracker side:

```python
from transport.connection import ConnectionManager

mgr = ConnectionManager(on_connection=tracker_on_connection)
await mgr.listen("0.0.0.0", 6767)
print("tracker on", mgr.address)
```

Client side:

```python
mgr = ConnectionManager(on_connection=unused_on_client)
await mgr.listen("0.0.0.0", 0)          # a peer also accepts direct connections
conn = await mgr.connect(tracker_addr, username)
await conn.control_stream.send(login_bytes)
```

`connect()` performs HELLO/HELLO_ACK and returns only when the connection is
established. It raises `ConnectionError` after about 2.5 s if the peer does
not answer.

## Streams

| ID | Purpose | Who creates it |
|---|---|---|
| 0 | control (`LOGIN`, `CHAT_SEND`, `DM` control, etc.) | automatic on both sides |
| 1 | global chat (tracker connection) | either side: `conn.open_stream(1)` |
| 2, 4, … | data (DM, files) if this side initiated the connection | `conn.open_stream()` |
| 3, 5, … | data if this side accepted the connection | `conn.open_stream()` |

Both sides may create stream 1 independently; that is normal — a stream ID is
a logical name, and each endpoint has its own object for it.

## Calls

```python
await stream.send(data: bytes)     # reliable and in-order within this stream
data = await stream.recv()         # one complete message
stream = conn.open_stream()        # next 2/4/... or 3/5/... ID
stream = conn.open_stream(1)       # explicit reserved ID
```

`send()` fragments automatically. It may wait when the peer's window is full,
so it belongs in a task, not in a callback.

`recv()` returns exactly one message, never a partial one. After the
connection closes it raises `ConnectionError`, so always wrap it:

```python
try:
    msg = await stream.recv()
except ConnectionError:
    break
```

## Callbacks (sync — do not block inside them)

```python
conn.on_stream(lambda stream: asyncio.create_task(read_stream(stream)))
conn.on_connection(lambda conn: tracker_register(conn))       # manager-level
conn.on_close(lambda conn: tracker_remove(conn))
```

- `ConnectionManager(on_connection=...)` fires for inbound connections only.
- `Connection.on_stream` fires when the peer opens a new stream.
- `Connection.on_close` fires on local close or incoming FIN.

## Guarantees

- reliable, ordered delivery **per stream**
- streams are independent: a stalled file stream does not block chat
- message boundaries are preserved; empty messages are valid
- flow control: the sender respects the receiver's advertised window
- retransmission: RTO and fast retransmit (3 duplicate ACKs / SACK evidence)
- liveness: PING every 5 s; a silent peer is closed after 15 s and `on_close` fires
- close: both `recv()` and a blocked `send()` wake with `ConnectionError`

## Useful fields

`conn.peer_addr` — UDP address of the peer (use for `PEER_JOINED`).
`conn.peer_name` — name sent in HELLO.
`conn.conn_id` — local connection ID (not needed by app code).
`mgr.address` — local `[ip, port]` (use for `LOGIN_OK.your_addr`).

## Caveat before the final demo

**No congestion control (AIMD).** Multiple big streams can oversubscribe the
shared socket; a small message can wait an RTO if its single packet is lost.
Fine for the demo; documented as future work.

## Do not

- build `Packet` objects or ACKs in app code
- call `conn._transmit`, `stream.on_data`, `stream.on_ack`
- share one `Stream` for interleaved unrelated messages; open a new stream
- send an entire file as one `send()`; chunk it (e.g. 64 KB per message)
