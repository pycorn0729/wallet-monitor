"""DB helpers shared by routes and background tasks."""

import json

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import KV, KIND_TRANSFER, Transfer, Wallet


def kv_get(db: Session, key: str) -> str | None:
    row = db.get(KV, key)
    return row.value if row else None


def kv_set(db: Session, key: str, value) -> None:
    row = db.get(KV, key)
    if row:
        row.value = str(value)
    else:
        db.add(KV(key=key, value=str(value)))


def tracked_addresses(db: Session) -> set[str]:
    return set(db.scalars(select(Wallet.address)))


def wallet_map(db: Session) -> dict[str, Wallet]:
    return {w.address: w for w in db.scalars(select(Wallet))}


def _find_duplicate(db: Session, row: dict) -> Transfer | None:
    """Same movement already stored (possibly imported from the other source)?"""
    q = select(Transfer).where(
        Transfer.block_number == row["block_number"], Transfer.event_index == row["event_index"]
    )
    found = db.scalars(q).first()
    if found or not row.get("extrinsic_id"):
        return found
    q = select(Transfer).where(
        Transfer.extrinsic_id == row["extrinsic_id"],
        Transfer.kind == row["kind"],
        Transfer.from_address == row["from_address"],
        Transfer.to_address == row["to_address"],
    )
    if row["kind"] == KIND_TRANSFER:
        q = q.where(Transfer.amount_rao == row["amount_rao"])
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


def unapproved_query(addresses: list[str] | None = None):
    q = select(func.count(Transfer.id)).where(Transfer.approved.is_(False))
    if addresses is not None:
        q = q.where(or_(Transfer.from_address.in_(addresses), Transfer.to_address.in_(addresses)))
    return q


def unapproved_count(db: Session, addresses: list[str] | None = None) -> int:
    if addresses is not None and not addresses:
        return 0
    return db.scalar(unapproved_query(addresses)) or 0


def unapproved_by_address(db: Session) -> dict[str, int]:
    """Unapproved transfer count per address (a transfer counts for both sides)."""
    counts: dict[str, int] = {}
    for col in (Transfer.from_address, Transfer.to_address):
        q = select(col, func.count(Transfer.id)).where(Transfer.approved.is_(False)).group_by(col)
        for addr, n in db.execute(q):
            counts[addr] = counts.get(addr, 0) + n
    # Self-transfers (e.g. moving stake between own subnets) were counted twice.
    q = (
        select(Transfer.from_address, func.count(Transfer.id))
        .where(Transfer.approved.is_(False), Transfer.from_address == Transfer.to_address)
        .group_by(Transfer.from_address)
    )
    for addr, n in db.execute(q):
        counts[addr] -= n
    return counts


def stake_positions(wallet: Wallet) -> list[dict]:
    try:
        return json.loads(wallet.stake_detail or "[]")
    except ValueError:
        return []
