"""Builds connection-graph data (nodes + aggregated edges) from the app database."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ss58
from .models import KINDS, RAO_PER_TAO, Transfer
from .services import label_map, touches, unapproved_by_address, unapproved_by_pair, visible, wallet_map


def build_graph(db: Session, address: str, kinds: set[str], limit: int) -> dict:
    """Transfers touching `address`."""
    q = (
        select(Transfer)
        .where(Transfer.kind.in_(kinds), visible(), touches([address]))
        .order_by(Transfer.block_number.desc())
        .limit(limit)
    )
    transfers = db.scalars(q).all()

    edges: dict[tuple, dict] = {}
    for t in transfers:
        e = edges.setdefault(
            (t.from_address, t.to_address, t.kind),
            {"count": 0, "amount_rao": 0, "alpha_rao": 0, "netuids": set(), "items": [], "last": None},
        )
        e["count"] += 1
        e["amount_rao"] += t.amount_rao
        e["alpha_rao"] += t.alpha_rao or 0
        e["netuids"].update(n for n in (t.origin_netuid, t.destination_netuid) if n is not None)
        if t.timestamp and (e["last"] is None or t.timestamp > e["last"]):
            e["last"] = t.timestamp
        if len(e["items"]) < 25:
            e["items"].append(
                {
                    "transfer_id": t.id,
                    "approved": t.approved,
                    "block": t.block_number,
                    "extrinsic_id": t.extrinsic_id,
                    "amount": t.amount,
                    "alpha": t.alpha,
                    "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                }
            )

    wallets = wallet_map(db)
    labels = label_map(db)
    # Approval only applies to transfers involving a registered wallet.
    unapproved_pairs = unapproved_by_pair(db, {k for k in edges if k[0] in wallets or k[1] in wallets})
    unapproved_nodes = unapproved_by_address(db)

    node_ids = {address}
    for f, t, _ in edges:
        node_ids.update((f, t))

    nodes = []
    for a in node_ids:
        w, lbl = wallets.get(a), labels.get(a)
        nodes.append(
            {
                "id": a,
                "label": w.name if w else lbl.label if lbl else ss58.short(a),
                "registered": bool(w),
                "labeled": bool(lbl) and not w,
                "tag": lbl.label if lbl else None,
                "note": lbl.note if lbl else None,
                "wallet_id": w.id if w else None,
                "owner": w.owner.username if w else None,
                "purpose": w.purpose if w else None,
                "total": w.total if w else None,
                "unapproved": unapproved_nodes.get(a, 0) if w else 0,
                "center": a == address,
            }
        )

    edge_list = [
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
        for (f, t, k), e in edges.items()
    ]
    return {"nodes": nodes, "edges": edge_list, "address": address, "rows": len(transfers), "truncated": len(transfers) >= limit}


def ensure_kinds(raw: str | None) -> set[str]:
    kinds = {k for k in (raw or "").split(",") if k in KINDS}
    return kinds or set(KINDS)
