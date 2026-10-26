"""Command line helpers.

    python -m app.cli create-user alice --admin --approver
    python -m app.cli set-password alice
"""

import argparse
import getpass
import sys

from .auth import hash_password
from .db import SessionLocal, init_db
from .models import User


def _ask_password() -> str:
    pw = getpass.getpass("Password: ")
    if len(pw) < 8 or pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords must match and be at least 8 characters.")
    return pw


def main():
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user")
    c.add_argument("username")
    c.add_argument("--admin", action="store_true")
    c.add_argument("--approver", action="store_true")
    c.add_argument("--password", help="non-interactive (avoid on shared machines)")
    p = sub.add_parser("set-password")
    p.add_argument("username")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == args.username).first()
        if args.cmd == "create-user":
            if user:
                sys.exit(f"User {args.username} already exists.")
            pw = args.password or _ask_password()
            db.add(User(username=args.username, password_hash=hash_password(pw), is_admin=args.admin, is_approver=args.approver))
            db.commit()
            print(f"Created {args.username}.")
        elif args.cmd == "set-password":
            if not user:
                sys.exit(f"No user {args.username}.")
            user.password_hash = hash_password(_ask_password())
            db.commit()
            print("Password updated.")


if __name__ == "__main__":
    main()
