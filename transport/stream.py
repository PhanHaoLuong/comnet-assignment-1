import asyncio
from dataclasses import dataclass
from typing import Protocol

from common.constants import (
    ACK_HEAD,
    Flags,
    INITIAL_RTO,
    MAX_PAYLOAD,
    MAX_RETRANSMITS,
    MAX_SACK_RANGES,
    RTO_MAX,
    RTO_MIN,
    SACK_RANGE,
    WINDOW_SIZE,
)
from common.packet import Packet


class ConnectionLike(Protocol):
    """The part of Connection that Stream is allowed to use."""

    def send_data(
        self, stream_id: int, seq: int, payload: bytes, eom: bool = False
    ) -> int:
        ...

    def send_ack(
        self, stream_id: int, cum_ack: int, window: int, ranges: list[tuple[int, int]]
    ) -> None:
        ...


@dataclass
class SentPacket:
    seq: int
    pkt_num: int
    payload: bytes
    eom: bool
    sent_at: float
    retries: int = 0


def pack_ack(cum_ack: int, window: int, ranges: list[tuple[int, int]]) -> bytes:
    """Encode an ACK payload. Ranges are inclusive (start, end)."""
    head = ACK_HEAD.pack(cum_ack, window, len(ranges))
    return head + b"".join(SACK_RANGE.pack(start, end) for start, end in ranges)


def parse_ack(payload: bytes) -> tuple[int, int, list[tuple[int, int]]]:
    """Decode an ACK payload; raises ValueError on malformed input."""
    if len(payload) < ACK_HEAD.size:
        raise ValueError("ACK payload too short")
    cum_ack, window, n_sack = ACK_HEAD.unpack_from(payload)
    if len(payload) != ACK_HEAD.size + n_sack * SACK_RANGE.size:
        raise ValueError("ACK length does not match n_sack")
    ranges = [
        SACK_RANGE.unpack_from(payload, ACK_HEAD.size + i * SACK_RANGE.size)
        for i in range(n_sack)
    ]
    return cum_ack, window, ranges


class Stream:
    def __init__(self, stream_id: int, conn: ConnectionLike, window: int = WINDOW_SIZE):
        self.conn = conn
        self.stream_id = stream_id
        self.loop = asyncio.get_running_loop()

        # --- send side ---
        self.next_seq = 0
        self.unacked: dict[int, SentPacket] = {}
        self.window = window
        self.peer_window = window          # learned from ACKs
        self.window_slots = asyncio.Semaphore(window)
        self.window_event = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.last_cum_ack = 0              # highest cumulative ACK seen
        self.srtt = None                   # seconds (Pass 2)
        self.rttvar = None                 # seconds (Pass 2)
        self.rto = INITIAL_RTO
        self.timer = None                  # asyncio.TimerHandle
        self.dup_acks = 0                  # fast retransmit (Pass 3)
        self._fast_retx_seq = None         # hole already fast-retransmitted

        # --- receive side ---
        self.next_expected = 0
        self.reorder: dict[int, tuple[bytes, bool]] = {}  # seq -> (payload, eom)
        self.assembling = bytearray()
        self.recv_queue = asyncio.Queue()

        self.closed = False

    async def send(self, msg: bytes) -> None:
        """Fragment `msg` and send it. Returns once all packets are queued."""
        if self.closed:
            raise ConnectionError("stream is closed")

        chunks = [
            msg[i:i + MAX_PAYLOAD] for i in range(0, len(msg), MAX_PAYLOAD)
        ] or [b""]

        async with self.send_lock:
            for i, chunk in enumerate(chunks):
                eom = i == len(chunks) - 1

                # The receiver's window is a sequence range, not a packet count:
                # do not send beyond cum_ack + advertised window.
                while self.next_seq >= self.last_cum_ack + self.peer_window:
                    if self.closed:
                        raise ConnectionError("stream is closed")
                    self.window_event.clear()
                    await self.window_event.wait()

                await self.window_slots.acquire()
                if self.closed:
                    raise ConnectionError("stream is closed")
                try:
                    seq = self.next_seq
                    pkt_num = self.conn.send_data(self.stream_id, seq, chunk, eom)
                except Exception:
                    self.window_slots.release()
                    raise

                self.unacked[seq] = SentPacket(
                    seq=seq,
                    pkt_num=pkt_num,
                    payload=chunk,
                    eom=eom,
                    sent_at=self.loop.time(),
                )
                self.next_seq += 1
                self._arm_timer()

    async def recv(self) -> bytes:
        """Return the next complete application message.

        Raises ConnectionError once the stream is closed and all queued
        messages have been delivered.
        """
        msg = await self.recv_queue.get()
        if msg is None:
            raise ConnectionError("stream is closed")
        return msg

    # ------------------------------------------------------------------ receive

    def on_data(self, pkt: Packet) -> None:
        seq = pkt.seq
        eom = bool(pkt.flags & Flags.EOM)

        # Duplicate: already delivered, or waiting in the reorder buffer.
        # ACK again so the sender can stop retransmitting it.
        if seq < self.next_expected or seq in self.reorder:
            self._send_ack()
            return

        # Outside the receive window: drop without ACK (spec 3.7).
        if seq >= self.next_expected + self.window:
            return

        self.reorder[seq] = (pkt.payload, eom)

        # Deliver every fragment that is now contiguous.
        while self.next_expected in self.reorder:
            payload, last = self.reorder.pop(self.next_expected)
            self.assembling.extend(payload)
            self.next_expected += 1
            if last:
                self.recv_queue.put_nowait(bytes(self.assembling))
                self.assembling.clear()

        self._send_ack()

    def _send_ack(self) -> None:
        ranges = []
        for seq in sorted(self.reorder):
            if ranges and seq == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], seq)
            else:
                ranges.append((seq, seq))

        free = max(0, self.window - len(self.reorder))
        self.conn.send_ack(
            self.stream_id,
            self.next_expected,
            free,
            ranges[:MAX_SACK_RANGES],
        )

    # -------------------------------------------------------------------- sender

    def _arm_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if not self.unacked:
            return
        oldest = self.unacked[min(self.unacked)]
        delay = max(0.0, oldest.sent_at + self.rto - self.loop.time())
        self.timer = self.loop.call_later(delay, self._on_timeout)

    def _on_timeout(self) -> None:
        self.timer = None
        if self.closed or not self.unacked:
            return

        seq = min(self.unacked)
        if self.unacked[seq].retries >= MAX_RETRANSMITS:
            self.close()
            return

        self._retransmit(seq, fast=False)
        self._arm_timer()

    def _retransmit(self, seq: int, fast: bool = False) -> None:
        """Resend one seq. Same seq/payload/EOM, new pkt_num."""
        sent = self.unacked.get(seq)
        if sent is None or self.closed:
            return
        try:
            sent.pkt_num = self.conn.send_data(
                self.stream_id, sent.seq, sent.payload, sent.eom
            )
        except Exception:
            self.close()
            return

        sent.sent_at = self.loop.time()
        sent.retries += 1
        if not fast:
            self.rto = min(self.rto * 2, RTO_MAX)

    def on_ack(self, pkt: Packet) -> None:
        try:
            cum_ack, window, ranges = parse_ack(pkt.payload)
        except ValueError:
            return  # a malformed ACK must never kill the loop

        # Ignore stale ACKs: they must not move counters backwards.
        if cum_ack < self.last_cum_ack:
            return

        if cum_ack > self.last_cum_ack:
            self.last_cum_ack = cum_ack
            self.dup_acks = 0
            self._fast_retx_seq = None
        else:
            self.dup_acks += 1

        self.peer_window = window
        self.window_event.set()
        now = self.loop.time()

        # Everything below cum_ack, plus anything covered by a SACK range.
        acked = [
            seq
            for seq in sorted(self.unacked)
            if seq < cum_ack
            or any(start <= seq <= end for start, end in ranges)
        ]

        # SACKs showing >= 3 packets above the hole are as good as 3 dup ACKs,
        # and survive duplicate-ACK loss.
        sacked_above = sum(
            max(0, end - max(start, cum_ack + 1) + 1) for start, end in ranges
        )

        sample = None
        for seq in acked:
            sent = self.unacked.pop(seq)
            self.window_slots.release()
            if sent.retries == 0:
                sample = now - sent.sent_at  # one sample per ACK (Karn)

        if sample is not None:
            self._rtt_sample(sample)

        # Three dup ACKs, or SACK evidence of 3+ later packets: repair now.
        if (
            (self.dup_acks >= 3 or sacked_above >= 3)
            and cum_ack in self.unacked
            and self._fast_retx_seq != cum_ack
        ):
            self._fast_retx_seq = cum_ack
            self._retransmit(cum_ack, fast=True)

        self._arm_timer()

    def _rtt_sample(self, sample):
        if self.srtt is None:
            self.srtt = sample
            self.rttvar = sample / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt = 0.875 * self.srtt + 0.125 * sample
        self.rto = min(RTO_MAX, max(RTO_MIN, self.srtt + 4 * self.rttvar))

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.recv_queue.put_nowait(None)   # wake recv() with an error
        self.window_event.set()            # wake send() waiting for the window
        for _ in range(self.window):
            self.window_slots.release()    # wake send() blocked on the semaphore
