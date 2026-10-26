"""DB helpers shared by routes and background tasks."""

import json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import KV, AddressLabel, Transfer, Wallet


def paginate(db: Session, query, page: int, size: int = 50) -> dict:
    """One page of `query` (already ordered) plus what the pager macro needs."""
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    pages = max(1, (total + size - 1) // size)
    page = min(max(1, page), pages)
    rows = db.scalars(query.offset((page - 1) * size).limit(size)).all()
    return {"rows": rows, "total": total, "page": page, "pages": pages}


def kv_get(db: Session, key: str) -> str | None:
    row = db.get(KV, key)
    return row.value if row else None


def kv_set(db: Session, key: str, value) -> None:
    row = db.get(KV, key)
    if row:
        row.value = str(value)
    else:
        db.add(KV(key=key, value=str(value)))


def wallet_map(db: Session) -> dict[str, Wallet]:
    return {w.address: w for w in db.scalars(select(Wallet))}


def label_map(db: Session) -> dict[str, AddressLabel]:
    return {lbl.address: lbl for lbl in db.scalars(select(AddressLabel))}


def visible():
    """Transfers below the configured minimum are hidden everywhere."""
    return Transfer.amount_rao >= get_settings().min_transfer_rao


def touches(addresses):
    """Transfer has `addresses` (a list or a select of addresses) on either side."""
    return or_(Transfer.from_address.in_(addresses), Transfer.to_address.in_(addresses))


def related():
    """Transfer touches at least one registered wallet."""
    return touches(select(Wallet.address).scalar_subquery())


def _find_duplicate(db: Session, row: dict) -> Transfer | None:
    """Same movement already stored?

    Rows from the same source are identified by (block, event position). Chain and Taostats
    rows don't share event numbering, so across sources match on extrinsic + parties + amount.
    """
    q = select(Transfer).where(
        Transfer.block_number == row["block_number"], Transfer.event_index == row["event_index"]
    )
    found = db.scalars(q).first()
    if found or not row.get("extrinsic_id"):
        return found
    q = select(Transfer).where(
        Transfer.extrinsic_id == row["extrinsic_id"],
        Transfer.source != row.get("source", "chain"),
        Transfer.kind == row["kind"],
        Transfer.from_address == row["from_address"],
        Transfer.to_address == row["to_address"],
        Transfer.amount_rao == row["amount_rao"],
    )
    return db.scalars(q).first()


def store_transfers(db: Session, rows: list[dict]) -> int:
    """Insert transfer dicts (keys = Transfer columns), skipping ones already stored."""
    added = 0
    for row in rows:
        existing = _find_duplicate(db, row)
        if existing:
            # Fill in details the other source may have lacked.
            for key in (
                "extrinsic_hash", "alpha_rao", "timestamp", "hotkey", "block_hash",
                "origin_netuid", "destination_netuid",
            ):
                if getattr(existing, key) is None and row.get(key) is not None:
                    setattr(existing, key, row[key])
            continue
        db.add(Transfer(**row))
        db.flush()
        added += 1
    db.commit()
    return added


def unapproved_count(db: Session, addresses: list[str] | None = None) -> int:
    """Unapproved related transfers, optionally only those touching `addresses`."""
    if addresses is not None and not addresses:
        return 0
    q = select(func.count(Transfer.id)).where(Transfer.approved.is_(False), visible(), related())
    if addresses is not None:
        q = q.where(touches(addresses))
    return db.scalar(q) or 0


def unapproved_by_address(db: Session) -> dict[str, int]:
    """Unapproved transfer count per registered wallet address (a transfer counts for both sides)."""
    wallets = select(Wallet.address).scalar_subquery()
    base = (Transfer.approved.is_(False), visible())
    counts: dict[str, int] = {}
    for col in (Transfer.from_address, Transfer.to_address):
        q = select(col, func.count(Transfer.id)).where(*base, col.in_(wallets)).group_by(col)
        for addr, n in db.execute(q):
            counts[addr] = counts.get(addr, 0) + n
    # Self-transfers (e.g. moving stake between own subnets) were counted twice.
    q = (
        select(Transfer.from_address, func.count(Transfer.id))
        .where(*base, Transfer.from_address == Transfer.to_address, Transfer.from_address.in_(wallets))
        .group_by(Transfer.from_address)
    )
    for addr, n in db.execute(q):
        counts[addr] -= n
    return counts


def unapproved_by_pair(db: Session, pairs: set[tuple[str, str, str]]) -> dict[tuple, int]:
    """Unapproved count per (from, to, kind); callers only pass pairs touching a registered wallet."""
    if not pairs:
        return {}
    cond = or_(
        *(and_(Transfer.from_address == f, Transfer.to_address == t, Transfer.kind == k) for f, t, k in pairs)
    )
    q = (
        select(Transfer.from_address, Transfer.to_address, Transfer.kind, func.count(Transfer.id))
        .where(Transfer.approved.is_(False), visible(), cond)
        .group_by(Transfer.from_address, Transfer.to_address, Transfer.kind)
    )
    return {(f, t, k): n for f, t, k, n in db.execute(q)}


def stake_positions(wallet: Wallet) -> list[dict]:
    try:
        return json.loads(wallet.stake_detail or "[]")
    except ValueError:
        return []
