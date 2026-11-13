"""Handshake, packet routing, and stream ownership for one peer.

Layers above this file only see:

    mgr = ConnectionManager(on_connection=...)
    await mgr.listen(...)
    conn = await mgr.connect(addr, name)

    await conn.control_stream.send(data)
    msg = await conn.control_stream.recv()
    stream = conn.open_stream()          # 2, 4, ... (or 3, 5, ... as responder)

    conn.on_close(callback)
    conn.on_stream(callback)             # inbound stream arrived

No app code should touch HELLO/ACK/seq/pkt_num directly.
"""

import asyncio
import logging
import random
from dataclasses import replace
from enum import Enum
from typing import Callable, Optional

from common.constants import (
    HEADER,
    HEADER_SIZE,
    HELLO,
    HELLO_ACK,
    HELLO_RETRY_COUNT,
    HELLO_RETRY_INTERVAL,
    PEER_DEAD_S,
    PING_INTERVAL_S,
    PING_TOKEN,
    VERSION,
    Flags,
    PacketType,
)
from common.net import Addr, UdpEndpoint
from common.packet import Packet, PacketError, decode, encode

try:
    from transport.crypto import TAG_LEN, CryptoContext, CryptoError
except ModuleNotFoundError as exc:          # pragma: no cover - install hint
    if exc.name == "cryptography":
        raise ImportError(
            "transport encryption requires the 'cryptography' package; "
            "run: python3 -m pip install -r requirements.txt"
        ) from exc
    raise

from transport.stream import Stream, pack_ack

logger = logging.getLogger(__name__)

CONTROL_STREAM_ID = 0


def parse_hello(payload: bytes) -> tuple[int, str, bytes, bytes]:
    """Return (sender_conn_id, username, eph_pub, nonce) or raise ValueError."""
    if len(payload) < HELLO.size:
        raise ValueError("short HELLO payload")
    sender_id, eph_pub, nonce, name_len = HELLO.unpack_from(payload)
    if sender_id == 0:
        raise ValueError("HELLO sender_conn_id is 0")
    if len(payload) != HELLO.size + name_len:
        raise ValueError("HELLO name_len does not match payload")
    return sender_id, payload[HELLO.size:].decode("utf-8"), eph_pub, nonce


class ConnectionState(Enum):
    NEW = 1
    HANDSHAKING = 2
    ESTABLISHED = 3
    CLOSING = 4
    CLOSED = 5


class Connection:
    def __init__(self, endpoint: UdpEndpoint, peer_addr: Addr, conn_id: int,
                 is_initiator: bool, ping_interval: float = PING_INTERVAL_S,
                 dead_timeout: float = PEER_DEAD_S):
        self.endpoint = endpoint
        self.peer_addr = peer_addr
        self.conn_id = conn_id                 # our local ID; peer puts it in headers
        self.peer_conn_id = None               # their local ID; we put it in headers
        self.is_initiator = is_initiator
        self.next_pkt_num = 0
        self.crypto = CryptoContext(is_initiator=is_initiator)
        self.loop = asyncio.get_running_loop()
        self.state = ConnectionState.NEW
        self._established = None
        self.peer_name = None

        self.streams: dict[int, Stream] = {}
        self.control_stream: Optional[Stream] = None
        self.next_stream_id = 2 if is_initiator else 3

        self._close_callbacks: list[Callable[["Connection"], None]] = []
        self._stream_callbacks: list[Callable[[Stream], None]] = []

        self.ping_interval = ping_interval
        self.dead_timeout = dead_timeout
        self.last_seen = 0.0
        self._ping_token = 0
        self._keepalive_task: Optional[asyncio.Task] = None

    # -- public callbacks ---------------------------------------------------

    def on_close(self, callback: Callable[["Connection"], None]) -> None:
        self._close_callbacks.append(callback)

    def on_stream(self, callback: Callable[[Stream], None]) -> None:
        self._stream_callbacks.append(callback)

    # -- streams ------------------------------------------------------------

    def open_stream(self, stream_id: Optional[int] = None) -> Stream:
        if self.state is not ConnectionState.ESTABLISHED:
            raise RuntimeError("connection is not established")
        if stream_id is None:
            stream_id = self.next_stream_id
            self.next_stream_id += 2
        if stream_id in self.streams:
            raise ValueError(f"stream {stream_id} already exists")
        return self._new_stream(stream_id)

    def _new_stream(self, stream_id: int) -> Stream:
        stream = Stream(stream_id, self)
        self.streams[stream_id] = stream
        if stream_id != CONTROL_STREAM_ID:
            for callback in list(self._stream_callbacks):
                callback(stream)
        return stream

    def _stream_for_data(self, stream_id: int) -> Stream:
        stream = self.streams.get(stream_id)
        if stream is None:
            stream = self._new_stream(stream_id)
        return stream

    def _setup_control_stream(self) -> None:
        if CONTROL_STREAM_ID not in self.streams:
            self.control_stream = self._new_stream(CONTROL_STREAM_ID)

    # -- keepalive ----------------------------------------------------------

    def _start_keepalive(self) -> None:
        self.last_seen = self.loop.time()
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = self.loop.create_task(self._keepalive())

    async def _keepalive(self) -> None:
        """PING every interval; close after dead_timeout of silence."""
        try:
            while self.state is ConnectionState.ESTABLISHED:
                await asyncio.sleep(self.ping_interval)
                if self.state is not ConnectionState.ESTABLISHED:
                    return
                if self.loop.time() - self.last_seen > self.dead_timeout:
                    logger.debug("peer %s timed out", self.peer_addr)
                    self.close()
                    return
                self._ping_token = (self._ping_token + 1) & 0xFFFFFFFFFFFFFFFF
                try:
                    self._transmit(
                        PacketType.PING,
                        PING_TOKEN.pack(self._ping_token),
                        stream_id=CONTROL_STREAM_ID,
                    )
                except Exception:
                    self.close()
                    return
        except asyncio.CancelledError:
            raise

    # -- transport API used by Stream ---------------------------------------

    def send_data(self, stream_id: int, seq: int, payload: bytes,
                  eom: bool = False) -> int:
        if self.state is not ConnectionState.ESTABLISHED:
            raise RuntimeError("connection is not established")
        flags = Flags.EOM if eom else 0
        return self._transmit(
            PacketType.DATA, payload, stream_id=stream_id, seq=seq, flags=flags
        )

    def send_ack(self, stream_id: int, cum_ack: int, window: int,
                 ranges: list[tuple[int, int]]) -> None:
        if self.state is not ConnectionState.ESTABLISHED:
            return
        self._transmit(
            PacketType.ACK,
            pack_ack(cum_ack, window, ranges),
            stream_id=stream_id,
            seq=0,
        )

    def send(self, type_: PacketType, payload: bytes = b"", stream_id: int = 0,
             seq: int = 0, flags: int = 0) -> int:
        """Raw escape hatch for PING/FIN and transport tests."""
        if self.state is not ConnectionState.ESTABLISHED:
            raise RuntimeError("connection is not established")
        return self._transmit(
            type_, payload, stream_id=stream_id, seq=seq, flags=flags
        )

    # -- handshake ----------------------------------------------------------

    async def connect(self, name: str) -> None:
        self.state = ConnectionState.HANDSHAKING
        self._established = asyncio.get_running_loop().create_future()
        name_b = name.encode("utf-8")
        payload = (
            HELLO.pack(
                self.conn_id,
                self.crypto.public_bytes,
                self.crypto.local_nonce,
                len(name_b),
            )
            + name_b
        )
        for _ in range(HELLO_RETRY_COUNT):
            self._transmit(PacketType.HELLO, payload, conn_id=0)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._established),
                    timeout=HELLO_RETRY_INTERVAL,
                )
                self._setup_control_stream()
                self._start_keepalive()
                return
            except asyncio.TimeoutError:
                continue
        self.state = ConnectionState.CLOSED
        raise ConnectionError(
            f"failed to establish connection after {HELLO_RETRY_COUNT} attempts"
        )

    def accept(self, pkt: Packet) -> None:
        sender_id, name, peer_eph, peer_nonce = parse_hello(pkt.payload)
        self.peer_conn_id = sender_id
        self.peer_name = name

        # A duplicate HELLO only re-sends the ACK; it must never re-key an
        # established connection (a spoofed HELLO would desync the peer).
        if not self.crypto.is_established:
            try:
                self.crypto.establish(peer_eph, peer_nonce)
            except CryptoError as exc:
                logger.info(
                    "rejecting HELLO from %s: crypto handshake failed (%s); "
                    "peer may be running a pre-crypto build",
                    self.peer_addr, exc,
                )
                self.close()
                return

        self.state = ConnectionState.ESTABLISHED
        self._setup_control_stream()
        self._start_keepalive()
        self._transmit(
            PacketType.HELLO_ACK,
            HELLO_ACK.pack(
                self.conn_id, self.crypto.public_bytes, self.crypto.local_nonce
            ),
            conn_id=sender_id,
        )

    # -- incoming packets ---------------------------------------------------

    def on_packet(self, pkt: Packet) -> None:
        if self.state is ConnectionState.HANDSHAKING:
            if pkt.type == PacketType.HELLO_ACK:
                self._handle_hello_ack(pkt)
            return

        if self.state is ConnectionState.ESTABLISHED:
            if pkt.type in (PacketType.HELLO, PacketType.HELLO_ACK):
                return

            # After the handshake every packet must be encrypted. Deciding by
            # the ENC flag alone would let an attacker clear it and inject
            # plaintext, so plaintext is rejected outright.
            if self.crypto.is_established:
                if not pkt.flags & Flags.ENC:
                    logger.debug(
                        "dropping unencrypted %s from %s", pkt.type, self.peer_addr
                    )
                    return
                try:
                    plaintext = self.crypto.decrypt(
                        pkt.pkt_num, self._header_bytes(pkt), pkt.payload
                    )
                except CryptoError as exc:
                    logger.debug(
                        "dropping %s from %s: %s", pkt.type, self.peer_addr, exc
                    )
                    return
                pkt = replace(pkt, payload=plaintext)

            self.last_seen = self.loop.time()

            if pkt.type == PacketType.PING:
                if len(pkt.payload) == PING_TOKEN.size:
                    self._transmit(
                        PacketType.PONG, pkt.payload, stream_id=CONTROL_STREAM_ID
                    )
                return
            if pkt.type == PacketType.PONG:
                return

            if pkt.type == PacketType.DATA:
                self._stream_for_data(pkt.stream_id).on_data(pkt)
            elif pkt.type == PacketType.ACK:
                stream = self.streams.get(pkt.stream_id)
                if stream is not None:
                    stream.on_ack(pkt)
            elif pkt.type == PacketType.FIN:
                self._remote_close()
            return

        if pkt.type == PacketType.FIN and self.state is not ConnectionState.CLOSED:
            self._remote_close()

    def _handle_hello_ack(self, pkt: Packet) -> None:
        if pkt.conn_id != self.conn_id or len(pkt.payload) < HELLO_ACK.size:
            return
        sender_id, peer_eph, peer_nonce = HELLO_ACK.unpack_from(pkt.payload)
        if sender_id == 0:
            return

        if not self.crypto.is_established:
            try:
                self.crypto.establish(peer_eph, peer_nonce)
            except CryptoError as exc:
                logger.debug("crypto handshake failed with %s: %s",
                             self.peer_addr, exc)
                if self._established is not None and not self._established.done():
                    self._established.set_exception(exc)
                self.close()
                return

        self.peer_conn_id = sender_id
        self.state = ConnectionState.ESTABLISHED
        if self._established is not None and not self._established.done():
            self._established.set_result(True)

    # -- close --------------------------------------------------------------

    def close(self) -> None:
        if self.state in (ConnectionState.CLOSING, ConnectionState.CLOSED):
            return
        if self.state is ConnectionState.ESTABLISHED:
            self.state = ConnectionState.CLOSING
            try:
                self._transmit(PacketType.FIN, b"\x00")
            except Exception:
                logger.debug("failed to send FIN", exc_info=True)
        self._teardown()

    def _remote_close(self) -> None:
        if self.state is ConnectionState.CLOSED:
            return
        self._teardown()

    def _teardown(self) -> None:
        self.state = ConnectionState.CLOSED
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        for stream in list(self.streams.values()):
            stream.close()
        self.streams.clear()
        # keep self.control_stream pointing at the (closed) stream so callers
        # holding it get ConnectionError from recv() instead of AttributeError
        callbacks, self._close_callbacks = self._close_callbacks, []
        for callback in callbacks:
            try:
                callback(self)
            except Exception:
                logger.exception("on_close callback failed")

    # -- packet construction ------------------------------------------------

    @staticmethod
    def _header_bytes(pkt: Packet) -> bytes:
        """Rebuild the exact 19-byte header received, for use as AAD."""
        return HEADER.pack(
            pkt.version,
            pkt.type,
            pkt.flags,
            pkt.conn_id,
            pkt.pkt_num,
            pkt.stream_id,
            pkt.seq,
            pkt.length,
        )

    def _transmit(self, type_: PacketType, payload: bytes = b"", conn_id=None,
                  stream_id: int = 0, seq: int = 0, flags: int = 0) -> int:
        if conn_id is None:
            if self.peer_conn_id is None:
                raise RuntimeError("peer conn_id is not known yet")
            conn_id = self.peer_conn_id

        pkt_num = self.next_pkt_num
        self.next_pkt_num += 1

        # HELLO/HELLO_ACK carry the key material, so they stay plaintext.
        # Everything else is encrypted once keys exist (including PING, ACK
        # and FIN: plaintext control packets would be injectable).
        encrypt = (
            self.crypto.is_established
            and type_ not in (PacketType.HELLO, PacketType.HELLO_ACK)
        )
        if encrypt:
            flags |= Flags.ENC
            # The length field covers the ciphertext (plaintext + tag), and
            # the final header is the AAD, so build it before encrypting.
            header = HEADER.pack(
                VERSION, int(type_), int(flags), conn_id, pkt_num,
                stream_id, seq, len(payload) + TAG_LEN,
            )
            payload = self.crypto.encrypt(pkt_num, header, payload)

        pkt = Packet(
            type=int(type_),
            conn_id=conn_id,
            pkt_num=pkt_num,
            stream_id=stream_id,
            seq=seq,
            payload=payload,
            flags=flags,
        )
        wire = encode(pkt)
        if encrypt and wire[:HEADER_SIZE] != header:
            raise RuntimeError("encrypted packet header does not match its AAD")
        self.endpoint.send(wire, self.peer_addr)
        return pkt_num


class ConnectionManager:
    def __init__(self, on_connection: Optional[Callable[[Connection], None]] = None,
                 ping_interval: float = PING_INTERVAL_S,
                 dead_timeout: float = PEER_DEAD_S):
        self.connections: dict[int, Connection] = {}
        self.by_hello: dict[tuple[Addr, int], Connection] = {}
        self.on_connection = on_connection or (lambda conn: None)
        self.endpoint: Optional[UdpEndpoint] = None
        self.ping_interval = ping_interval
        self.dead_timeout = dead_timeout

    @property
    def address(self) -> Optional[Addr]:
        return self.endpoint.address if self.endpoint is not None else None

    def _generate_conn_id(self) -> int:
        while True:
            conn_id = random.randint(1, 2**32 - 1)
            if conn_id not in self.connections:
                return conn_id

    def _register(self, conn: Connection) -> None:
        self.connections[conn.conn_id] = conn
        conn.on_close(self._drop)

    def _drop(self, conn: Connection) -> None:
        self.connections.pop(conn.conn_id, None)
        for key, value in list(self.by_hello.items()):
            if value is conn:
                del self.by_hello[key]

    async def listen(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self.endpoint = UdpEndpoint(self.on_datagram)
        await self.endpoint.open(host, port)

    async def connect(self, peer_addr: Addr, name: str) -> Connection:
        if self.endpoint is None:
            raise RuntimeError("call listen() before connect()")
        conn = Connection(
            self.endpoint,
            peer_addr,
            self._generate_conn_id(),
            is_initiator=True,
            ping_interval=self.ping_interval,
            dead_timeout=self.dead_timeout,
        )
        self._register(conn)
        try:
            await conn.connect(name)
        except Exception:
            self._drop(conn)
            raise
        return conn

    def on_datagram(self, data: bytes, addr: Addr) -> None:
        try:
            pkt = decode(data)
        except PacketError as exc:
            logger.debug("dropping bad datagram from %s: %s", addr, exc)
            return

        conn = self.connections.get(pkt.conn_id)
        if conn is not None:
            if addr != conn.peer_addr:
                # Bind established connections to the address that created
                # them; otherwise a guessed conn_id is enough to inject.
                logger.debug(
                    "dropping packet for conn %s from unexpected %s",
                    conn.conn_id, addr,
                )
                return
            conn.on_packet(pkt)
            return

        if pkt.type == PacketType.HELLO and pkt.conn_id == 0:
            try:
                sender_id, _name, _eph, _nonce = parse_hello(pkt.payload)
            except (ValueError, UnicodeDecodeError) as exc:
                logger.debug("dropping bad HELLO from %s: %s", addr, exc)
                return

            key = (addr, sender_id)
            conn = self.by_hello.get(key)
            if conn is None:
                conn = Connection(
                    self.endpoint,
                    addr,
                    self._generate_conn_id(),
                    is_initiator=False,
                    ping_interval=self.ping_interval,
                    dead_timeout=self.dead_timeout,
                )
                self.by_hello[key] = conn
                self._register(conn)
                conn.accept(pkt)
                # A failed crypto handshake closes the connection; do not
                # hand a dead session (control_stream is None) to app code.
                if conn.state is ConnectionState.ESTABLISHED:
                    self.on_connection(conn)
            else:
                conn.accept(pkt)      # duplicate HELLO: resend the ACK
