"""DB helpers shared by routes and background tasks."""

import json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import KV, AddressLabel, SyncedRange, Transfer, Wallet


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


def mark_synced(db: Session, start: int, end: int, start_time=None, end_time=None) -> None:
    """Record blocks start..end as scanned, merged with the ranges they overlap or touch.
    The times are those of blocks start and end. Not committed: the caller commits it
    together with its own progress."""
    rows = db.scalars(
        select(SyncedRange).where(SyncedRange.start_block <= end + 1, SyncedRange.end_block >= start - 1)
    ).all()
    low, low_time, high, high_time = start, start_time, end, end_time
    for r in rows:
        if r.start_block < low or (r.start_block == low and low_time is None):
            low, low_time = r.start_block, r.start_time
        if r.end_block > high or (r.end_block == high and high_time is None):
            high, high_time = r.end_block, r.end_time
    keep = rows[0] if rows else SyncedRange()
    for r in rows[1:]:
        db.delete(r)
    keep.start_block, keep.start_time, keep.end_block, keep.end_time = low, low_time, high, high_time
    if not rows:
        db.add(keep)


def unsynced_span(db: Session, start: int, end: int) -> tuple[int, int]:
    """The first run of blocks within start..end not scanned yet, as (first, last).
    first > end when all of start..end is already synced."""
    covering = db.scalar(
        select(SyncedRange.end_block).where(SyncedRange.start_block <= start, SyncedRange.end_block >= start)
    )
    if covering is not None:
        start = covering + 1  # ranges never touch, so the block after one is unscanned
    following = db.scalar(
        select(func.min(SyncedRange.start_block)).where(SyncedRange.start_block > start, SyncedRange.start_block <= end)
    )
    return start, following - 1 if following is not None else end


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
