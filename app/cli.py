"""Command line helpers.

    python -m app.cli create-user alice --admin --approver
    python -m app.cli set-password alice
    python -m app.cli copy-from-sqlite wallet_monitor.db   # move data into the configured database
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


def copy_from_sqlite(path: str) -> None:
    """One-off move of all data from an SQLite file into the configured (empty) database,
    keeping ids, then advancing the id sequences so new rows don't collide."""
    from sqlalchemy import create_engine, func, select, text

    from .db import Base, engine

    if engine.url.get_backend_name() == "sqlite":
        sys.exit("WM_DATABASE_URL points at SQLite; set it to the target database first.")
    source = create_engine(f"sqlite:///{path}")
    tables = Base.metadata.sorted_tables  # parents before children (foreign keys)
    with engine.connect() as dst:
        if any(dst.scalar(select(func.count()).select_from(t)) for t in tables):
            sys.exit("Target database is not empty; refusing to copy over existing data.")
    with source.connect() as src, engine.begin() as dst:
        for table in tables:
            rows = [dict(r._mapping) for r in src.execute(select(table))]
            for i in range(0, len(rows), 5000):
                dst.execute(table.insert(), rows[i:i + 5000])
            if "id" in table.c and engine.url.get_backend_name() == "postgresql":
                dst.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                    f"(SELECT MAX(id) FROM {table.name}) IS NOT NULL)"
                ))
            print(f"{table.name}: {len(rows)} rows")
    print("Done.")


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
    m = sub.add_parser("copy-from-sqlite", help="copy every table from an SQLite file into WM_DATABASE_URL")
    m.add_argument("path")
    args = parser.parse_args()

    init_db()
    if args.cmd == "copy-from-sqlite":
        copy_from_sqlite(args.path)
        return
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
