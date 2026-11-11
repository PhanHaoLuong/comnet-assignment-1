import struct
import asyncio
import random
import logging
from enum import Enum
from typing import Callable
from common.net import UdpEndpoint, Addr
from common.packet import Packet, encode, decode
from common.constants import PacketType
from common.constants import HELLO_RETRY_COUNT, HELLO_RETRY_INTERVAL, HELLO, HELLO_ACK

logger = logging.getLogger(__name__)

class ConnectionState(Enum):
    NEW = 1
    HANDSHAKING = 2
    ESTABLISHED = 3
    CLOSING = 4
    CLOSED = 5

class Connection:
    def __init__(self, endpoint: UdpEndpoint, peer_addr: Addr, conn_id: int):
        self.endpoint = endpoint
        self.peer_addr = peer_addr
        self.conn_id = conn_id
        self.peer_conn_id = None
        self.next_pkt_num = 0
        self.state = ConnectionState.NEW
        self._established = None
        self.peer_name = None

    def _transmit(self, type_: PacketType, payload: bytes = b"", conn_id = None, stream_id: int = 0, seq: int = 0, flags: int = 0) -> None:
        if conn_id is None:
            conn_id = self.peer_conn_id or 0
        pkt = Packet(
            type=int(type_),
            conn_id=conn_id,
            pkt_num=self.next_pkt_num,
            stream_id=stream_id,
            seq=seq,
            payload=payload,
            flags=flags
        )
        self.next_pkt_num += 1
        encoded_pkt = encode(pkt)
        self.endpoint.send(encoded_pkt, self.peer_addr)

    async def connect(self, name: str) -> None:
        self.state = ConnectionState.HANDSHAKING
        self._established = asyncio.get_running_loop().create_future()
        name_b = name.encode('utf-8')
        payload = HELLO.pack(self.conn_id, b"\0"*32, b"\0"*16, len(name_b)) + name_b
        for _ in range(HELLO_RETRY_COUNT):  # Retry up to 5 times
            self._transmit(PacketType.HELLO, payload, conn_id=0)
            try:
                await asyncio.wait_for(asyncio.shield(self._established), timeout=HELLO_RETRY_INTERVAL)
                return
            except asyncio.TimeoutError:
                continue
        self.state = ConnectionState.CLOSED
        raise ConnectionError("Failed to establish connection after 5 attempts.")

    def accept(self, pkt: Packet) -> None:
        sender_id, _eph, _nonce, name_len = HELLO.unpack_from(pkt.payload)

        if sender_id == 0 or HELLO.size + name_len != len(pkt.payload):
            raise ValueError("Invalid HELLO packet received.")

        self.peer_conn_id = sender_id
        self.peer_name = pkt.payload[HELLO.size:].decode('utf-8')

        self.state = ConnectionState.ESTABLISHED

        self._transmit(PacketType.HELLO_ACK, HELLO_ACK.pack(self.conn_id, b"\x00"*32, b"\x00"*16), conn_id=sender_id)
        
    def on_packet(self, pkt: Packet) -> None:
        if pkt.type == PacketType.HELLO_ACK and self.state is ConnectionState.HANDSHAKING:
            if pkt.conn_id != self.conn_id:
                return
            sender_id, _eph, _nonce = HELLO_ACK.unpack_from(pkt.payload)
            if sender_id == 0:
                return
            self.peer_conn_id = sender_id
            self.state = ConnectionState.ESTABLISHED
            self._established.set_result(True)
        elif pkt.type == PacketType.FIN:
            self.state = ConnectionState.CLOSED

    def send(self, type, payload: bytes = b"", stream_id: int = 0, seq: int = 0, flags: int = 0) -> None:
        if self.state != ConnectionState.ESTABLISHED:
            raise RuntimeError("Cannot send packets on a non-established connection.")
        self._transmit(type, payload, stream_id=stream_id, conn_id=self.peer_conn_id, seq=seq, flags=flags)

    def close(self) -> None:
        if self.state in (ConnectionState.CLOSING, ConnectionState.CLOSED):
            return
        self.state = ConnectionState.CLOSING
        # Send a FIN packet
        self._transmit(PacketType.FIN, b"\x00")
        self.state = ConnectionState.CLOSED

class ConnectionManager:
    def __init__(self, on_connection: Callable[[Connection], None] = None):
        self.connections = {}
        self.by_hello = {}
        self.on_connection = on_connection or (lambda c: None)
        self.endpoint = None

    def _generate_conn_id(self) -> int:
        return random.randint(1, 2**32 - 1)

    async def listen(self, host: str = "0.0.0.0", port: int = 0) -> None:
        self.endpoint = UdpEndpoint(self.on_datagram)
        await self.endpoint.open(host, port)

    async def connect(self, peer_addr: Addr, name: str) -> Connection:
        conn_id = self._generate_conn_id()
        connection = Connection(self.endpoint, peer_addr, conn_id)
        self.connections[conn_id] = connection
        try:
            await connection.connect(name)
        except Exception:
            self.connections.pop(connection.conn_id, None)
            raise
        return connection

    def on_datagram(self, data: bytes, addr: Addr) -> None:
        try:
            pkt = decode(data)
        except Exception as e:
            logger.debug(f"Failed to decode packet from {addr}: {e}")
            return

        conn_id = pkt.conn_id
        if conn_id in self.connections:
            connection = self.connections[conn_id]
            connection.on_packet(pkt)
        else:
            # Handle new connection
            if pkt.type == PacketType.HELLO and pkt.conn_id == 0:
                try:
                    sender_id, _eph, _nonce, name_len = HELLO.unpack_from(pkt.payload)
                except struct.error:
                    return
                if sender_id == 0 or HELLO.size + name_len != len(pkt.payload):
                    return
                try:
                    pkt.payload[HELLO.size:].decode("utf-8")
                except UnicodeDecodeError:
                    return

                key = (addr, sender_id)
                conn = self.by_hello.get(key)
                if conn is None:
                    conn = Connection(self.endpoint, addr, self._generate_conn_id())
                    self.by_hello[key] = conn
                    self.connections[conn.conn_id] = conn
                    conn.accept(pkt)
                    self.on_connection(conn)
                else:
                    conn.accept(pkt)
                return
                