"""Admin tool: the only way accounts are created.

    python -m app.admin add alice            # prompts for a password
    python -m app.admin passwd alice
    python -m app.admin remove alice
    python -m app.admin list
    python -m app.admin --file data/accounts.json add bob

A running tracker picks up changes automatically (AccountStore.refresh).
"""

import argparse
import getpass
import sys

from app import auth
from common.constants import ACCOUNTS_FILE


def _read_password(args: argparse.Namespace) -> str:
    if args.password is not None:
        return args.password       # convenient for scripts; visible in shell history
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise ValueError("passwords do not match")
    return first


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    parser.add_argument("--file", default=ACCOUNTS_FILE, help="accounts file")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("add", "passwd"):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--password")
    sub.add_parser("remove").add_argument("username")
    sub.add_parser("list")

    args = parser.parse_args(argv)

    try:
        accounts = auth.load_accounts(args.file)
        if args.cmd == "list":
            for name in sorted(accounts):
                print(name)
            return 0
        if args.cmd == "add":
            name = auth.add_account(accounts, args.username, _read_password(args))
            msg = f"created '{name}'"
        elif args.cmd == "passwd":
            name = auth.set_password(accounts, args.username, _read_password(args))
            msg = f"password updated for '{name}'"
        else:
            name = auth.remove_account(accounts, args.username)
            msg = f"removed '{name}'"
        auth.save_accounts(accounts, args.file)
        print(msg)
        return 0
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
