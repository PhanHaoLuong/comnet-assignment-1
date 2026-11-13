"""Unit tests for transport/crypto.py (Level 1 transport encryption).

Run from the repo root:

    python3 -m unittest discover -s tests -p 'test_crypto.py'
    python3 -m unittest tests.test_crypto

Requires the 'cryptography' package (see requirements.txt).
"""

import unittest

from cryptography.hazmat.primitives.asymmetric import x25519

from common.constants import HEADER, HEADER_SIZE, Flags, PacketType, VERSION
from transport.crypto import (
    TAG_LEN,
    CryptoContext,
    CryptoError,
    ReplayError,
    build_nonce,
)

NONCE_A = bytes(range(16))
NONCE_B = bytes(range(16, 32))

# Known-answer values, generated with the fixed keys/nonces below.
PUB_A = "a4e09292b651c278b9772c569f5fa9bb13d906b46ab68c9df9dc2b4409f8a209"
PUB_B = "ce8d3ad1ccb633ec7b70c17814a5c76ecd029685050d344745ba05870e587d59"
A_SEND_KEY = "ee3923caa0eb602b6e5bb34826b69ad278ae2cca7af99d9c6736279f89b732f9"
A_RECV_KEY = "9b89ef4100f8a7efbd23331ea14ad113e63c24021bdcaaa46f1e90a904dd73c4"
FIXED_HEADER = bytes.fromhex("0104021122334400000007000000000000001c")
FIXED_NONCE = bytes.fromhex("010000000000000000000007")
FIXED_CT = bytes.fromhex(
    "c36d2cea4ed648c3fe96f93add5b5379ab99633724209f645e77c404"
)


def _key(byte):
    return x25519.X25519PrivateKey.from_private_bytes(bytes([byte]) * 32)


def _pair():
    a = CryptoContext(True, private_key=_key(1), local_nonce=NONCE_A)
    b = CryptoContext(False, private_key=_key(2), local_nonce=NONCE_B)
    a.establish(b.public_bytes, b.local_nonce)
    b.establish(a.public_bytes, a.local_nonce)
    return a, b


class KnownAnswerTests(unittest.TestCase):
    def test_public_keys(self):
        a, b = _pair()
        self.assertEqual(a.public_bytes.hex(), PUB_A)
        self.assertEqual(b.public_bytes.hex(), PUB_B)

    def test_directional_keys(self):
        a, b = _pair()
        self.assertEqual(a.send_key.hex(), A_SEND_KEY)
        self.assertEqual(a.recv_key.hex(), A_RECV_KEY)
        self.assertEqual(b.send_key, a.recv_key)
        self.assertEqual(b.recv_key, a.send_key)
        self.assertNotEqual(a.send_key, a.recv_key)

    def test_nonce_layout(self):
        nonce = build_nonce(b"\x01", 7)
        self.assertEqual(len(nonce), 12)
        self.assertEqual(nonce, FIXED_NONCE)
        self.assertEqual(nonce[0], 1)
        self.assertEqual(nonce[-4:], (7).to_bytes(4, "big"))

    def test_nonce_rejects_out_of_range_pkt_num(self):
        with self.assertRaises(ValueError):
            build_nonce(b"\x01", 2 ** 32)
        with self.assertRaises(ValueError):
            build_nonce(b"\x01", -1)

    def test_ciphertext(self):
        a, _ = _pair()
        self.assertEqual(a.encrypt(7, FIXED_HEADER, b"fixed vector"), FIXED_CT)
        self.assertEqual(TAG_LEN, 16)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_both_directions(self):
        a, b = _pair()
        header = HEADER.pack(VERSION, int(PacketType.DATA), int(Flags.EOM),
                             0x11223344, 0, 0, 0, 5 + 16)
        ct = a.encrypt(0, header, b"hello")
        self.assertEqual(b.decrypt(0, header, ct), b"hello")

        ct = b.encrypt(0, header, b"reply")
        self.assertEqual(a.decrypt(0, header, ct), b"reply")

    def test_empty_payload(self):
        a, b = _pair()
        header = bytes(HEADER_SIZE)
        self.assertEqual(b.decrypt(0, header, a.encrypt(0, header, b"")), b"")

    def test_tampered_ciphertext_is_rejected(self):
        a, b = _pair()
        ct = bytearray(a.encrypt(0, bytes(HEADER_SIZE), b"payload"))
        ct[3] ^= 0xFF
        with self.assertRaises(CryptoError):
            b.decrypt(0, bytes(HEADER_SIZE), bytes(ct))

    def test_tampered_header_is_rejected(self):
        a, b = _pair()
        header = bytearray(HEADER_SIZE)
        ct = a.encrypt(0, bytes(header), b"payload")
        header[5] ^= 0xFF
        with self.assertRaises(CryptoError):
            b.decrypt(0, bytes(header), ct)


class ReplayTests(unittest.TestCase):
    def test_exact_replay_rejected(self):
        a, b = _pair()
        header = bytes(HEADER_SIZE)
        ct = a.encrypt(7, header, b"x")
        b.decrypt(7, header, ct)
        with self.assertRaises(ReplayError):
            b.decrypt(7, header, ct)

    def test_reordering_accepted_once(self):
        a, b = _pair()
        header = bytes(HEADER_SIZE)
        cts = {n: a.encrypt(n, header, bytes([n])) for n in range(10)}
        for n in (0, 2, 1, 4, 3):
            self.assertEqual(b.decrypt(n, header, cts[n]), bytes([n]))
        for n in (1, 3):
            with self.assertRaises(ReplayError):
                b.decrypt(n, header, cts[n])

    def test_packet_older_than_window_rejected(self):
        a, b = _pair()
        header = bytes(HEADER_SIZE)
        old = a.encrypt(0, header, b"old")
        b.decrypt(64, header, a.encrypt(64, header, b"new"))
        with self.assertRaises(ReplayError):
            b.decrypt(0, header, old)


class TransmitTests(unittest.TestCase):
    def test_pkt_num_must_increase(self):
        a, _ = _pair()
        header = bytes(HEADER_SIZE)
        a.encrypt(5, header, b"a")
        with self.assertRaises(CryptoError):
            a.encrypt(5, header, b"b")
        with self.assertRaises(CryptoError):
            a.encrypt(4, header, b"c")

    def test_header_length_is_validated(self):
        a, b = _pair()
        with self.assertRaises(CryptoError):
            a.encrypt(0, b"\x00" * (HEADER_SIZE - 1), b"x")
        ct = a.encrypt(0, bytes(HEADER_SIZE), b"x")
        with self.assertRaises(CryptoError):
            b.decrypt(0, b"\x00" * (HEADER_SIZE + 1), ct)


class HandshakeTests(unittest.TestCase):
    def test_short_peer_key_rejected(self):
        ctx = CryptoContext(True, local_nonce=NONCE_A)
        with self.assertRaises(CryptoError):
            ctx.establish(b"\x00" * 31, NONCE_B)

    def test_low_order_peer_key_rejected_as_crypto_error(self):
        ctx = CryptoContext(True, local_nonce=NONCE_A)
        with self.assertRaises(CryptoError):
            ctx.establish(b"\x00" * 32, NONCE_B)

    def test_establish_twice_rejected(self):
        a, b = _pair()
        with self.assertRaises(CryptoError):
            a.establish(b.public_bytes, b.local_nonce)

    def test_local_nonce_length_validated(self):
        with self.assertRaises(ValueError):
            CryptoContext(True, local_nonce=b"\x00" * 15)

    def test_ephemeral_contexts_differ(self):
        x = CryptoContext(True)
        y = CryptoContext(True)
        self.assertNotEqual(x.public_bytes, y.public_bytes)
        self.assertNotEqual(x.local_nonce, y.local_nonce)


if __name__ == "__main__":
    unittest.main()
