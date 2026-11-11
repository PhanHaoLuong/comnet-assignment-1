from dataclasses import dataclass
import struct
from constants import HEADER_SIZE, MAX_PAYLOAD, VERSION
HEADER = struct.Struct("!BBBIIHIH")
class PacketError(ValueError):
    pass

@dataclass(frozen=True)
class Packet:
    type: int
    conn_id: int
    pkt_num: int
    stream_id: int
    seq: int
    payload: bytes = b""
    flags: int = 0
    version: int = VERSION
    @property
    def length(self) -> int:
        return len(self.payload)
    
def encode(packet: Packet) -> bytes:
    if packet.version != VERSION:
        raise PacketError(f"Invalid packet version: {packet.version}")
    
    if packet.type not in range(1, 9):
        raise PacketError(f"Invalid packet type: {packet.type}")
    
    if packet.flags & ~0x03:
        raise PacketError(f"Invalid packet flags: {packet.flags}")
    
    if len(packet.payload) > MAX_PAYLOAD:
        raise PacketError(f"Payload too large: {len(packet.payload)} bytes")

    length = len(packet.payload)
    try:
        header = HEADER.pack(
            packet.version,
            packet.type,
            packet.flags,
            packet.conn_id,
            packet.pkt_num,
            packet.stream_id,
            packet.seq,
            length
        )
    except struct.error as e:
        raise PacketError(f"Error packing header: {e}")
    return header + packet.payload

def decode(data: bytes) -> Packet:
    if len(data) < HEADER_SIZE:
        raise PacketError("Data is too short to decode a packet.")

    version, type_, flags, conn_id, pkt_num, stream_id, seq, length = HEADER.unpack_from(data)
    payload = data[HEADER_SIZE:]

    if version != VERSION:
        raise PacketError(f"Invalid packet version: {version}")
    if type_ not in range(1, 9):
        raise PacketError(f"Invalid packet type: {type_}")
    if flags & ~0x03:
        raise PacketError(f"Invalid packet flags: {flags}")
    if length > MAX_PAYLOAD:
        raise PacketError(f"Payload too large: {length} bytes")
    if length != len(payload):
        raise PacketError(f"Payload length mismatch: expected {length}, got {len(payload)}")
    
    return Packet(
        version=version,
        type=type_,
        flags=flags,
        conn_id=conn_id,
        pkt_num=pkt_num,
        stream_id=stream_id,
        seq=seq,
        payload=payload
    )

p = Packet(type=1, conn_id=7, pkt_num=1, stream_id=2, seq=0, payload=b"Hello, World!", flags=0)
print(encode(p).hex(" "))
print(decode(encode(p)))