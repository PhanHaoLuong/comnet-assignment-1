"""Protocol constants. Never hard-code these numbers elsewhere."""

import re
import struct
from enum import IntEnum, IntFlag
from typing import Final

VERSION: Final = 1
HEADER_SIZE: Final = 19
MAX_DATAGRAM: Final = 1200
MAX_PAYLOAD: Final = MAX_DATAGRAM - HEADER_SIZE
HEADER: Final = struct.Struct("!BBBIIHIH")

# --- accounts / auth -------------------------------------------------------
USERNAME_PATTERN = re.compile(r"[a-z0-9_]{3,20}")
MAX_PASSWORD_BYTES = 128            # UTF-8 bytes; stops oversized-LOGIN abuse

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000         # OWASP recommendation for PBKDF2-SHA256
SALT_SIZE = 16
HASH_SIZE = 32

ACCOUNTS_FILE = "data/accounts.json"
# ---------------------------------------------------------------------------

class PacketType(IntEnum):
    HELLO = 1
    HELLO_ACK = 2
    AUTH = 3
    DATA = 4
    ACK = 5
    PING = 6
    PONG = 7
    FIN = 8


class Flags(IntFlag):
    EOM = 0x01  # end of application message
    ENC = 0x02  # payload is encrypted


# Handshake payloads (level 0 fills keys with zeros; layout never changes)
HELLO: Final = struct.Struct("!I32s16sB")     # sender_id, eph_pub, nonce, name_len + name
HELLO_ACK: Final = struct.Struct("!I32s16s")  # sender_id, eph_pub, nonce

# ACK payload: cum_ack, window, n_sack, then n_sack x (start, end) inclusive
ACK_HEAD: Final = struct.Struct("!IHB")
SACK_RANGE: Final = struct.Struct("!II")
MAX_SACK_RANGES: Final = 8

# Timers and windows
HELLO_RETRY_COUNT: Final = 5
HELLO_RETRY_INTERVAL: Final = 0.5  # seconds
PING_INTERVAL_S: Final = 5.0       # heartbeat period
PEER_DEAD_S: Final = 15.0          # silence before closing
UNAUTH_TIMEOUT_S: Final = 30.0     # connected but never logged in
PING_TOKEN: Final = struct.Struct("!Q")
INITIAL_RTO: Final = 0.3           # seconds (spec: 300 ms)
RTO_MIN: Final = 0.05              # seconds
RTO_MAX: Final = 3.0               # seconds
MAX_RETRANSMITS: Final = 8
WINDOW_SIZE: Final = 32            # packets
