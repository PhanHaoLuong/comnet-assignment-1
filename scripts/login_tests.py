"""Tiny test client: log in to the tracker and print everything it sends.

Run from the repo root:

    python3 -m scripts.login_test --username alice
    python3 -m scripts.login_test --username alice --password secret --port 6767

Ctrl-C logs out. Open several terminals to watch PEER_JOINED / PEER_LEFT.
"""

import argparse
import asyncio
import getpass

from app import messages
from transport.connection import ConnectionManager


async def run(args: argparse.Namespace) -> None:
    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")

    mgr = ConnectionManager(on_connection=lambda conn: None)
    await mgr.listen("0.0.0.0", 0)             # a peer also accepts direct connections

    try:
        conn = await mgr.connect((args.host, args.port), args.username)
    except ConnectionError as exc:
        print(f"could not reach the tracker: {exc}")
        return
    stream = conn.control_stream

    login = messages.make("LOGIN", username=args.username, password=password)
    await stream.send(messages.encode(login))

    try:
        while True:
            reply = messages.decode(await stream.recv())
            print(reply)
            if reply["t"] == "LOGIN_OK":
                print(f"--- logged in as {args.username}; waiting for events ---")
    except ConnectionError:
        print("--- connection closed ---")
    except asyncio.CancelledError:
        try:
            await stream.send(messages.encode(messages.make("LOGOUT")))
        except Exception:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6767)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", help="prompted if omitted")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()