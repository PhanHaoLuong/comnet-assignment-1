"""Stream over real UDP (localhost), with optional loss.

    python3 scripts/stream_udp_demo.py
    python3 scripts/stream_udp_demo.py --loss 0.2 --bytes 50000 --seed 1

This is the integration check: Connection handshake, open_stream(), DATA/ACK
routing, fragmentation, and retransmission over real sockets.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.simulator import LossyLink
from transport.connection import ConnectionManager


async def main(args):
    inbound = []
    a = ConnectionManager()
    b = ConnectionManager(inbound.append)
    await a.listen("127.0.0.1", 0)
    await b.listen("127.0.0.1", 0)

    links = []
    if args.loss > 0:
        links.append(LossyLink(a.endpoint, loss=args.loss, delay_ms=10,
                               seed=args.seed))
        links.append(LossyLink(b.endpoint, loss=args.loss, delay_ms=10,
                               seed=args.seed + 1))

    try:
        alice = await asyncio.wait_for(a.connect(b.endpoint.address, "alice"), 5)
        bob = inbound[0]
        print(f"handshake: {alice.state.name} / {bob.state.name}")

        bob_streams = []
        bob.on_stream(bob_streams.append)

        stream_a = alice.open_stream()
        msg = os.urandom(args.bytes)
        await asyncio.wait_for(stream_a.send(msg), args.timeout)
        print(f"sent {len(msg)} bytes on stream {stream_a.stream_id}")

        for _ in range(1000):
            if bob_streams:
                break
            await asyncio.sleep(0.01)
        stream_b = bob_streams[0]
        got = await asyncio.wait_for(stream_b.recv(), args.timeout)

        print(f"received {len(got)} bytes, match={got == msg}")
        print(f"alice unacked={len(stream_a.unacked)} "
              f"bob next_expected={stream_b.next_expected}")
        for index, link in enumerate(links):
            print(f"link {index}: {link}")

        alice.close()
        await asyncio.sleep(0.1)
        try:
            await asyncio.wait_for(stream_b.recv(), 1)
        except ConnectionError as exc:
            print(f"receiver woke on close: {exc}")

        assert got == msg
    finally:
        for link in links:
            link.detach()
        a.endpoint.close()
        b.endpoint.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument("--bytes", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=20.0)
    asyncio.run(main(parser.parse_args()))
