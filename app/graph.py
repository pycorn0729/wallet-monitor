"""Builds connection-graph data (nodes + aggregated edges) for the graph page."""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from . import ss58
from .models import KINDS, RAO_PER_TAO, Transfer
from .services import unapproved_by_address, wallet_map
from .taostats import TaostatsClient


def _db_rows(db: Session, address: str | None, kinds: set[str], limit: int) -> list[dict]:
    q = select(Transfer).where(Transfer.kind.in_(kinds)).order_by(Transfer.block_number.desc()).limit(limit)
    if address:
        q = q.where(or_(Transfer.from_address == address, Transfer.to_address == address))
    return [
        {
            "kind": t.kind,
            "from_address": t.from_address,
            "to_address": t.to_address,
            "amount_rao": t.amount_rao,
            "alpha_rao": t.alpha_rao,
            "block_number": t.block_number,
            "extrinsic_id": t.extrinsic_id,
            "timestamp": t.timestamp,
            "origin_netuid": t.origin_netuid,
            "destination_netuid": t.destination_netuid,
        }
        for t in db.scalars(q)
    ]


def _unapproved_by_pair(db: Session, pairs: set[tuple[str, str, str]]) -> dict[tuple, int]:
    if not pairs:
        return {}
    cond = or_(
        *(and_(Transfer.from_address == f, Transfer.to_address == t, Transfer.kind == k) for f, t, k in pairs)
    )
    q = (
        select(Transfer.from_address, Transfer.to_address, Transfer.kind, func.count(Transfer.id))
        .where(Transfer.approved.is_(False), cond)
        .group_by(Transfer.from_address, Transfer.to_address, Transfer.kind)
    )
    return {(f, t, k): n for f, t, k, n in db.execute(q)}


async def build_graph(
    db: Session,
    taostats: TaostatsClient,
    address: str | None,
    source: str,
    kinds: set[str],
    limit: int,
) -> dict:
    if source == "taostats":
        if not address:
            raise ValueError("Pick an address to load from Taostats.")
        rows = await taostats.history(address, kinds, limit)
    else:
        rows = _db_rows(db, address, kinds, limit)

    # Aggregate per (from, to, kind).
    edges: dict[tuple, dict] = {}
    seen: set[tuple] = set()
    for r in rows:
        dedupe = (r.get("extrinsic_id"), r["kind"], r["from_address"], r["to_address"], r["amount_rao"])
        if r.get("extrinsic_id") and dedupe in seen:
            continue
        seen.add(dedupe)
        key = (r["from_address"], r["to_address"], r["kind"])
        e = edges.setdefault(
            key, {"count": 0, "amount_rao": 0, "alpha_rao": 0, "netuids": set(), "items": [], "last": None}
        )
        e["count"] += 1
        e["amount_rao"] += r["amount_rao"] or 0
        e["alpha_rao"] += r.get("alpha_rao") or 0
        for n in (r.get("origin_netuid"), r.get("destination_netuid")):
            if n is not None:
                e["netuids"].add(n)
        ts = r.get("timestamp")
        if ts and (e["last"] is None or ts > e["last"]):
            e["last"] = ts
        if len(e["items"]) < 25:
            e["items"].append(
                {
                    "block": r["block_number"],
                    "extrinsic_id": r.get("extrinsic_id"),
                    "amount": (r["amount_rao"] or 0) / RAO_PER_TAO,
                    "alpha": None if r.get("alpha_rao") is None else r["alpha_rao"] / RAO_PER_TAO,
                    "timestamp": ts.isoformat() if ts else None,
                }
            )

    wallets = wallet_map(db)
    unapproved_pairs = _unapproved_by_pair(db, set(edges))
    unapproved_nodes = unapproved_by_address(db)

    # Link extrinsic ids to locally stored transfers so the UI can open them.
    ext_ids = {i["extrinsic_id"] for e in edges.values() for i in e["items"] if i["extrinsic_id"]}
    local = {}
    if ext_ids:
        q = select(Transfer.extrinsic_id, Transfer.id, Transfer.approved).where(Transfer.extrinsic_id.in_(ext_ids))
        local = {x: (tid, ok) for x, tid, ok in db.execute(q)}

    node_ids = {address} if address else set()
    for f, t, _ in edges:
        node_ids.update((f, t))

    nodes = []
    for a in node_ids:
        w = wallets.get(a)
        nodes.append(
            {
                "id": a,
                "label": w.name if w else ss58.short(a),
                "registered": bool(w),
                "wallet_id": w.id if w else None,
                "owner": w.owner.username if w else None,
                "purpose": w.purpose if w else None,
                "total": w.total if w else None,
                "unapproved": unapproved_nodes.get(a, 0) if w else 0,
                "center": a == address,
            }
        )

    edge_list = []
    for (f, t, k), e in edges.items():
        for item in e["items"]:
            tid, ok = local.get(item["extrinsic_id"], (None, None))
            item["transfer_id"], item["approved"] = tid, ok
        edge_list.append(
            {
                "id": f"{f}|{t}|{k}",
                "from": f,
                "to": t,
                "kind": k,
                "count": e["count"],
                "amount": e["amount_rao"] / RAO_PER_TAO,
                "alpha": e["alpha_rao"] / RAO_PER_TAO,
                "netuids": sorted(e["netuids"]),
                "last": e["last"].isoformat() if e["last"] else None,
                "unapproved": unapproved_pairs.get((f, t, k), 0),
                "items": e["items"],
            }
        )
    return {"nodes": nodes, "edges": edge_list, "source": source, "address": address, "rows": len(rows)}


def ensure_kinds(raw: str | None) -> set[str]:
    kinds = {k for k in (raw or "").split(",") if k in KINDS}
    return kinds or set(KINDS)
