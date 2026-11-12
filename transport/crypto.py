"""Level 1 transport encryption: X25519 key exchange, HKDF-SHA256, AES-256-GCM."""

import os
import struct
from threading import Lock
from typing import Optional, Tuple

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16
HANDSHAKE_NONCE_LEN = 16
REPLAY_WINDOW_SIZE = 64

ROLE_INITIATOR = b"\x01"
ROLE_RESPONDER = b"\x02"


class CryptoError(Exception):
    """Raised when decryption, authentication, or replay checks fail."""


class ReplayError(CryptoError):
    """Raised when a duplicate or out-of-window pkt_num is received."""


def generate_ephemeral_keypair() -> Tuple[x25519.X25519PrivateKey, bytes]:
    """Return (X25519 private key object, 32-byte raw public key)."""
    priv = x25519.X25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, pub_bytes


def build_nonce(role_byte: bytes, pkt_num: int) -> bytes:
    """Build 12-byte GCM nonce: 1B role + 7B zero pad + 4B big-endian pkt_num."""
    if pkt_num < 0 or pkt_num > 0xFFFFFFFF:
        raise ValueError(f"pkt_num out of 32-bit range: {pkt_num}")
    return role_byte + (b"\x00" * 7) + struct.pack("!I", pkt_num)


def derive_session_keys(
    own_priv: x25519.X25519PrivateKey,
    peer_pub_bytes: bytes,
    init_nonce: bytes,
    resp_nonce: bytes,
    is_initiator: bool,
) -> Tuple[bytes, bytes]:
    """Derive directional (tx_key, rx_key) via X25519 + HKDF-SHA256."""
    peer_pub = x25519.X25519PublicKey.from_public_bytes(peer_pub_bytes)
    shared_secret = own_priv.exchange(peer_pub)
    salt = init_nonce + resp_nonce

    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LEN * 2,
        salt=salt,
        info=b"p2p-miniquic-v1-session-keys",
    ).derive(shared_secret)

    init_to_resp = key_material[:KEY_LEN]
    resp_to_init = key_material[KEY_LEN:]
    return (init_to_resp, resp_to_init) if is_initiator else (resp_to_init, init_to_resp)


class CryptoContext:
    """Per-connection Level 1 cryptographic state + pkt_num replay window."""

    def __init__(self, is_initiator: bool, window_size: int = REPLAY_WINDOW_SIZE):
        self.is_initiator = is_initiator
        self.tx_role = ROLE_INITIATOR if is_initiator else ROLE_RESPONDER
        self.rx_role = ROLE_RESPONDER if is_initiator else ROLE_INITIATOR

        self._priv_key, self.public_bytes = generate_ephemeral_keypair()
        self.local_nonce: bytes = os.urandom(HANDSHAKE_NONCE_LEN)

        self.send_key: bytes = b""
        self.recv_key: bytes = b""
        self._tx_cipher: Optional[AESGCM] = None
        self._rx_cipher: Optional[AESGCM] = None
        self._tx_lock = Lock()
        self._last_tx_pkt_num = -1

        # Sliding replay window state
        self._window_size = window_size
        self._max_pkt_num: int = -1
        self._replay_bitmap: int = 0

    @property
    def is_established(self) -> bool:
        return self._tx_cipher is not None and self._rx_cipher is not None

    def establish(self, peer_pub_bytes: bytes, peer_nonce: bytes) -> None:
        """Complete X25519 key agreement using exchanged public keys and nonces."""
        if len(peer_pub_bytes) != KEY_LEN:
            raise CryptoError(f"Expected {KEY_LEN}B peer public key, got {len(peer_pub_bytes)}")
        if len(peer_nonce) != HANDSHAKE_NONCE_LEN:
            raise CryptoError(f"Expected {HANDSHAKE_NONCE_LEN}B peer nonce, got {len(peer_nonce)}")

        init_nonce = self.local_nonce if self.is_initiator else peer_nonce
        resp_nonce = peer_nonce if self.is_initiator else self.local_nonce

        self.send_key, self.recv_key = derive_session_keys(
            self._priv_key, peer_pub_bytes, init_nonce, resp_nonce, self.is_initiator
        )
        self._tx_cipher = AESGCM(self.send_key)
        self._rx_cipher = AESGCM(self.recv_key)

    def _check_replay(self, pkt_num: int) -> None:
        """Raise ReplayError if pkt_num was already seen or is behind the window."""
        if pkt_num < 0:
            raise ReplayError("Negative pkt_num")
        if pkt_num <= self._max_pkt_num:
            diff = self._max_pkt_num - pkt_num
            if diff >= self._window_size:
                raise ReplayError(f"pkt_num {pkt_num} older than replay window")
            if (self._replay_bitmap >> diff) & 1:
                raise ReplayError(f"Replayed pkt_num {pkt_num}")

    def _record_pkt_num(self, pkt_num: int) -> None:
        """Advance sliding replay window after packet authentication succeeds."""
        if pkt_num > self._max_pkt_num:
            shift = pkt_num - self._max_pkt_num
            if shift >= self._window_size:
                self._replay_bitmap = 1
            else:
                self._replay_bitmap = ((self._replay_bitmap << shift) | 1) & (
                    (1 << self._window_size) - 1
                )
            self._max_pkt_num = pkt_num
        else:
            diff = self._max_pkt_num - pkt_num
            self._replay_bitmap |= 1 << diff

    def encrypt(self, pkt_num: int, header: bytes, payload: bytes) -> bytes:
        """Encrypt using the 19B header as AAD; pkt_num must strictly increase."""
        if self._tx_cipher is None:
            raise RuntimeError("CryptoContext keys not established")
        nonce = build_nonce(self.tx_role, pkt_num)
        with self._tx_lock:
            if pkt_num <= self._last_tx_pkt_num:
                raise CryptoError(
                    f"Transmit pkt_num must increase; got {pkt_num} after "
                    f"{self._last_tx_pkt_num}"
                )
            ciphertext = self._tx_cipher.encrypt(
                nonce, payload, associated_data=header
            )
            self._last_tx_pkt_num = pkt_num
            return ciphertext

    def decrypt(self, pkt_num: int, header: bytes, payload: bytes) -> bytes:
        """Verify replay window, decrypt payload, and authenticate 19B header AAD."""
        if self._rx_cipher is None:
            raise RuntimeError("CryptoContext keys not established")
        self._check_replay(pkt_num)
        nonce = build_nonce(self.rx_role, pkt_num)
        try:
            plaintext = self._rx_cipher.decrypt(nonce, payload, associated_data=header)
        except InvalidTag as exc:
            raise CryptoError("AES-GCM authentication failed") from exc
        self._record_pkt_num(pkt_num)
        return plaintext


# ---------------------------------------------------------------------------
# Optional Level 2 Helpers (Ed25519)
# ---------------------------------------------------------------------------

class IdentitySigner:
    """Optional Level 2 Ed25519 identity keypair helper."""

    def __init__(self, priv_key: Optional[ed25519.Ed25519PrivateKey] = None):
        self.priv_key = priv_key or ed25519.Ed25519PrivateKey.generate()
        self.pub_bytes = self.priv_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def sign(self, data: bytes) -> bytes:
        return self.priv_key.sign(data)

    @staticmethod
    def verify(pub_bytes: bytes, signature: bytes, data: bytes) -> bool:
        try:
            pub = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
            pub.verify(signature, data)
            return True
        except (InvalidSignature, ValueError):
            return False