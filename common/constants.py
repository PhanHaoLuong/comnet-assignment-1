from enum import IntEnum, IntFlag

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
    EOM = 0x01  # End of Message
    ENC = 0x02  # Encrypted