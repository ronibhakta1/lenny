#!/usr/bin/env python3
"""Manage librarian (admin) accounts from the shell.

    make admin-create NAME="Ada Lovelace" EMAIL=you@example.org
    make admin-passwd USERNAME=ada

The first account created on an instance becomes its super user. Passwords are
asked for on the terminal, never taken from the command line.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lenny.core import librarians
from lenny.core.librarians import Librarian, LibrarianError
from lenny.core.db import session as db


def _ask_password() -> str:
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise LibrarianError("The passwords do not match.")
    return first


def cmd_create(args) -> int:
    first = db.query(Librarian).count() == 0
    row = librarians.create(args.name, args.email, _ask_password(), is_super=first, username=args.username)
    print(f"Created {'super user' if first else 'librarian'} '{row.name}' (sign in with '{row.email}' or '{row.username}').")
    return 0


def cmd_passwd(args) -> int:
    row = db.query(Librarian).filter(Librarian.username == args.username).first()
    if not row:
        raise LibrarianError(f"No librarian named '{args.username}'.")
    librarians.update(row.id, password=_ask_password())
    print(f"Password changed for '{row.username}'.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="add a librarian (the first one is the super user)")
    create.add_argument("--name", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--username", help="login handle; derived from the email when left out")
    create.set_defaults(fn=cmd_create)
    passwd = sub.add_parser("passwd", help="set a new password for a librarian")
    passwd.add_argument("--username", required=True)
    passwd.set_defaults(fn=cmd_passwd)
    args = parser.parse_args()
    try:
        return args.fn(args)
    except LibrarianError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
