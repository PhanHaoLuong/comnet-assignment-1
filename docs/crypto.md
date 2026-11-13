# Transport crypto (Level 1)

Status: **implemented, wired into `transport/connection.py`, unit-tested.**
Module: `transport/crypto.py`. Integration: `transport/connection.py`.
Constants: `common/constants.py` (`CRYPTO_TAG_LEN`, `MAX_STREAM_PAYLOAD`).

## Scope

- **X25519** ephemeral key agreement per connection.
- **HKDF-SHA256** for directional key derivation.
- **AES-256-GCM** for every packet after the handshake.
- Replay protection on `pkt_num`.

Not included (documented non-goals): peer identity/authentication, 0-RTT
resumption, connection migration, key update/ratchet, zeroization, congestion
control.

## Handshake

`HELLO` and `HELLO_ACK` are **plaintext** and carry public key material:

| Message | Fields |
|---|---|
| `HELLO` | `sender_id`, `eph_pub` (32 B), `nonce` (16 B), `name_len` + username |
| `HELLO_ACK` | `sender_id`, `eph_pub` (32 B), `nonce` (16 B) |

Each side generates one `CryptoContext` per `Connection`: a fresh X25519
keypair and a random 16-byte handshake nonce. `is_initiator` is fixed by who
called `connect()`.

The 16-byte handshake nonce is **salt material**, not the AES-GCM nonce. It is
never secret.

## Key derivation

```
shared      = X25519(own_private, peer_public)
salt        = initiator_nonce || responder_nonce
okm         = HKDF-SHA256(shared, salt, length=64,
                          info=b"p2p-miniquic-v1-session-keys")
init->resp  = okm[:32]
resp->init  = okm[32:]
```

| Side | TX key | RX key |
|---|---|---|
| initiator | `init->resp` | `resp->init` |
| responder | `resp->init` | `init->resp` |

Fresh ephemeral keys give forward secrecy. The nonces bind the keys to this
handshake and guarantee both sides derive the same material.

## Packet protection

- Cipher: AES-256-GCM. Ciphertext = plaintext + 16-byte tag.
- **Nonce (12 B)**: `role byte || 7 zero bytes || pkt_num (big-endian 32-bit)`.
  `role` is `0x01` for the initiator and `0x02` for the responder. The nonce is
  never transmitted; the receiver reconstructs it from the header.
- **AAD**: the final 19-byte packet header, with `Flags.ENC` already set and
  `length` set to the **ciphertext** length.
- `pkt_num` must strictly increase per sender and must never be reused.
  Retransmissions get a new `pkt_num`; `seq` stays the same and must not be
  used for the nonce.

## Size limits (why plaintext fragments are 1165 B)

Wire payload cap: `MAX_PAYLOAD = 1200 - 19 = 1181` (`common/packet.py` checks
this on encode and decode). AES-GCM adds 16 bytes, so plaintext fragments must
stop at `MAX_STREAM_PAYLOAD = 1181 - 16 = 1165`. `Stream.send` fragments at
that size; `MAX_PAYLOAD` stays 1181 for the packet layer.

## Replay window

Sliding 64-packet bitmap per direction. `pkt_num` is recorded **after** the
GCM tag verifies, so a forged packet cannot advance the window. Duplicates and
packets more than 64 behind the newest are rejected with `ReplayError`.
Decryption failures are dropped per packet; they must not close the
connection.

## Integration rules

1. `establish()` exactly once per connection; re-keying raises `CryptoError`.
2. Encrypt every packet except `HELLO`/`HELLO_ACK` — including `PING`, `ACK`
   and `FIN` (plaintext control packets are injectable).
3. After the handshake, reject packets without `Flags.ENC`; never decide
   whether to decrypt from the flag alone.
4. Decrypt before updating liveness (`last_seen`) or dispatching.
5. Drop `CryptoError` packets, do not tear the connection down.
6. One `CryptoContext` per connection; it is not thread-safe.

## Limitations

- **No peer authentication.** An active on-path attacker can MITM the
  unauthenticated X25519 exchange. Level 2 signed identities are a non-goal.
- **No 0-RTT.** Every connection is 1-RTT with fresh ephemeral keys.
- **No migration.** Connections are bound to the peer address.
- **No key update.** Long-lived connections use one key pair until close.
- **No stateless retry/cookie.** The responder allocates state on the first
  `HELLO`.

## Setup and tests

```bash
python3 -m pip install -r requirements.txt

python3 -m unittest discover -s tests -p 'test_crypto.py'
# or
python3 -m unittest tests.test_crypto
```

The suite covers known-answer derivation/ciphertext vectors, both-direction
round trip, tampered ciphertext/header, replay and window boundaries, nonce and
`pkt_num` rules, AAD length, and invalid peer keys.
