"""Multi-stream check: a big transfer must not block a small message.

    python3 scripts/multistream_demo.py
    python3 scripts/multistream_demo.py --bytes 2000000 --loss 0.1

Alice opens stream 2 (big) and stream 4 (small); Bob opens stream 3.
The small message is sent while the big one is in flight and must arrive
long before the big transfer finishes.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.simulator import LossyLink
from transport.connection import ConnectionManager


async def wait_stream(store, stream_id, timeout=10.0):
    for _ in range(int(timeout / 0.01)):
        if stream_id in store:
            return store[stream_id]
        await asyncio.sleep(0.01)
    raise TimeoutError(f"stream {stream_id} never arrived")


async def main(args):
    inbound = []
    a = ConnectionManager()
    b = ConnectionManager(inbound.append)
    await a.listen("127.0.0.1", 0)
    await b.listen("127.0.0.1", 0)

    links = []
    if args.loss > 0:
        links.append(LossyLink(a.endpoint, loss=args.loss, delay_ms=10, seed=args.seed))
        links.append(LossyLink(b.endpoint, loss=args.loss, delay_ms=10, seed=args.seed + 1))

    try:
        alice = await asyncio.wait_for(a.connect(b.endpoint.address, "alice"), 5)
        bob = inbound[0]
        print(f"handshake: {alice.state.name} / {bob.state.name}")

        bob_streams = {}
        alice_streams = {}
        bob.on_stream(lambda s: bob_streams.__setitem__(s.stream_id, s))
        alice.on_stream(lambda s: alice_streams.__setitem__(s.stream_id, s))

        big_stream = alice.open_stream()      # 2
        small_stream = alice.open_stream()    # 4
        another_big_stream = alice.open_stream()  # 6
        print(
            "alice opened streams:",
            [big_stream.stream_id, small_stream.stream_id, another_big_stream.stream_id],
        )

        payload = os.urandom(args.bytes)
        t0 = asyncio.get_running_loop().time()

        big_task = asyncio.create_task(big_stream.send(payload))
        await asyncio.sleep(0.05)             # let the big transfer get in flight

        small_t0 = asyncio.get_running_loop().time()
        await small_stream.send(b"ping")

        another_payload = os.urandom(args.bytes)
        another_t0 = asyncio.get_running_loop().time()

        another_big_task = asyncio.create_task(another_big_stream.send(another_payload))
        await asyncio.sleep(0.05)             # let the big transfer get in flight

        small_receiver = await wait_stream(bob_streams, small_stream.stream_id)
        small_msg = await asyncio.wait_for(small_receiver.recv(), timeout=10)
        small_elapsed = asyncio.get_running_loop().time() - small_t0

        big_receiver = await wait_stream(bob_streams, big_stream.stream_id)
        await asyncio.wait_for(big_task, timeout=args.timeout)
        got_big = await asyncio.wait_for(big_receiver.recv(), timeout=args.timeout)
        big_elapsed = asyncio.get_running_loop().time() - t0

        another_receiver = await wait_stream(bob_streams, another_big_stream.stream_id)
        await asyncio.wait_for(another_big_task, timeout=args.timeout)
        got_another_big = await asyncio.wait_for(another_receiver.recv(), timeout=args.timeout)
        another_elapsed = asyncio.get_running_loop().time() - another_t0

        assert small_msg == b"ping"
        assert got_big == payload
        assert got_another_big == another_payload
        assert small_elapsed < big_elapsed

        print(f"small message arrived in {small_elapsed*1000:.1f} ms")
        print(f"big transfer finished in {big_elapsed*1000:.1f} ms")
        print(f"another big transfer finished in {another_elapsed*1000:.1f} ms")
        print(f"no head-of-line blocking (small << big)")

        # The responder opens an odd stream; Alice must see it too.
        bob_stream = bob.open_stream()        # 3
        await bob_stream.send(b"from bob")
        alice_receiver = await wait_stream(alice_streams, bob_stream.stream_id)
        assert await asyncio.wait_for(alice_receiver.recv(), 5) == b"from bob"
        print(f"reverse direction on stream {bob_stream.stream_id}: OK")

        for index, link in enumerate(links):
            print(f"link {index}: {link}")
    finally:
        for link in links:
            link.detach()
        a.endpoint.close()
        b.endpoint.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bytes", type=int, default=2_000_000)
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=60.0)
    asyncio.run(main(parser.parse_args()))
