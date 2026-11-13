"""Manual P2P demo: run one process per user, connected through a tracker.

Terminal 1: python -m app.tracker_server --dev-auth --port 6767
Terminal 2: python scripts/p2p_chat_cli.py --user alice --tracker 127.0.0.1:6767
Terminal 3: python scripts/p2p_chat_cli.py --user bob --tracker 127.0.0.1:6767

Commands: /peers, /dm bob hello, /quit
"""

import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client.daemon import ClientDaemon


def parse_addr(value):
    host, colon, port = value.rpartition(":")
    if not colon or not port.isdecimal() or not host:
        raise argparse.ArgumentTypeError("use HOST:PORT")
    return (host, int(port))


async def show_events(daemon):
    while True:
        event = await daemon.events.get()
        kind = event.get("kind")
        if kind == "dm":
            msg = event["message"]
            if event.get("direction") == "incoming":
                print(f"\n[DM] {msg['from']} -> you: {msg['text']}", flush=True)
            else:
                print(f"\n[queued] you -> {msg['to']}: {msg['text']}", flush=True)
        elif kind == "peers":
            print("\n[online]", ", ".join(sorted(event["peers"])) or "(none)", flush=True)
        elif kind == "error":
            print("\n[error]", event.get("text"), flush=True)


async def main(args):
    daemon = ClientDaemon(args.tracker, listen_host=args.listen_host,
                          listen_port=args.listen_port)
    receiver = None
    try:
        await daemon.login(args.user, args.password)
        print(f"Logged in as {args.user}, UDP listening at {daemon.address}")
        print("Commands: /peers, /dm USER TEXT, /quit")
        receiver = asyncio.create_task(show_events(daemon))
        while True:
            try:
                line = (await asyncio.to_thread(input, "> ")).strip()
            except EOFError:
                break
            if line == "/quit":
                break
            if line == "/peers":
                print("online:", daemon.peers)
                continue
            if line.startswith("/dm "):
                parts = line.split(" ", 2)
                if len(parts) != 3:
                    print("syntax: /dm USER TEXT")
                    continue
                try:
                    await daemon.send_dm(parts[1], parts[2])
                except (RuntimeError, ValueError) as exc:
                    print("send failed:", exc)
                continue
            print("use /dm USER TEXT, /peers, or /quit")
    finally:
        if receiver is not None:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        await daemon.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--tracker", type=parse_addr, default=("127.0.0.1", 6767))
    parser.add_argument("--password", default=None)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    args = parser.parse_args()
    if args.password is None:
        args.password = getpass.getpass("Password (dev-auth: pw): ")
    asyncio.run(main(args))
