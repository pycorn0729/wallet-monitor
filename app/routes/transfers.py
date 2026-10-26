from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import Forbidden, current_user, require_approver
from ..db import get_db
from ..models import KINDS, Comment, Transfer, User, Wallet, utcnow
from ..services import related, visible, wallet_map
from ..web import flash, redirect, render

router = APIRouter()

PAGE_SIZE = 50


def _get_transfer(db: Session, transfer_id: int) -> Transfer:
    transfer = db.get(Transfer, transfer_id)
    if not transfer:
        raise Forbidden("Transfer not found.")
    return transfer


def _related_owner_ids(db: Session, transfer: Transfer) -> set[int]:
    q = select(Wallet.owner_id).where(Wallet.address.in_([transfer.from_address, transfer.to_address]))
    return set(db.scalars(q))


def can_comment(db: Session, user: User, transfer: Transfer) -> bool:
    return user.is_admin or user.id in _related_owner_ids(db, transfer)


def _set_approval(transfer: Transfer, approved: bool, user: User) -> None:
    transfer.approved = approved
    transfer.approved_by_id = user.id if approved else None
    transfer.approved_at = utcnow() if approved else None


@router.get("/transfers")
def transfer_list(
    request: Request,
    kind: str = "",
    status: str = "",
    owner: str = "",
    wallet: str = "",
    address: str = "",
    page: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    # History is about registered wallets; the rest of the network only feeds the graph.
    query = select(Transfer).where(related(), visible())
    if kind in KINDS:
        query = query.where(Transfer.kind == kind)
    if status == "unapproved":
        query = query.where(Transfer.approved.is_(False))
    elif status == "approved":
        query = query.where(Transfer.approved.is_(True))

    addresses: list[str] | None = None
    if wallet.isdigit():
        w = db.get(Wallet, int(wallet))
        addresses = [w.address] if w else []
    elif owner.isdigit():
        addresses = list(db.scalars(select(Wallet.address).where(Wallet.owner_id == int(owner))))
    if address.strip():
        a = address.strip()
        addresses = [a] if addresses is None else [x for x in addresses if x == a]
    if addresses is not None:
        query = query.where(or_(Transfer.from_address.in_(addresses), Transfer.to_address.in_(addresses)))

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    page = max(1, page)
    transfers = db.scalars(
        query.order_by(Transfer.block_number.desc(), Transfer.event_index.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    return render(
        request,
        "transfers.html",
        transfers=transfers,
        total=total,
        page=page,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        wallets=wallet_map(db),
        users=db.scalars(select(User).order_by(User.username)).all(),
        all_wallets=db.scalars(select(Wallet).order_by(Wallet.name)).all(),
        filters={"kind": kind, "status": status, "owner": owner, "wallet": wallet, "address": address},
    )


@router.get("/transfers/{transfer_id}")
def transfer_detail(
    request: Request, transfer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    transfer = _get_transfer(db, transfer_id)
    return render(
        request,
        "transfer_detail.html",
        t=transfer,
        wallets=wallet_map(db),
        is_related=bool(_related_owner_ids(db, transfer)),
        can_comment=can_comment(db, user, transfer),
    )


@router.post("/transfers/{transfer_id}/approve")
def transfer_approve(
    request: Request,
    transfer_id: int,
    approved: str = Form(...),
    user: User = Depends(require_approver),
    db: Session = Depends(get_db),
):
    transfer = _get_transfer(db, transfer_id)
    if not _related_owner_ids(db, transfer):
        raise Forbidden("Only transfers involving a registered wallet can be approved.")
    _set_approval(transfer, approved == "1", user)
    db.commit()
    return redirect(request.headers.get("referer") or f"/transfers/{transfer_id}")


@router.post("/transfers/bulk-approve")
async def transfer_bulk_approve(
    request: Request, user: User = Depends(require_approver), db: Session = Depends(get_db)
):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    approved = form.get("approved") == "1"
    for transfer in db.scalars(select(Transfer).where(Transfer.id.in_(ids), related())):
        _set_approval(transfer, approved, user)
    db.commit()
    flash(request, f"{'Approved' if approved else 'Un-approved'} {len(ids)} transfer(s).", "success")
    return redirect(request.headers.get("referer") or "/transfers")


@router.post("/transfers/{transfer_id}/comment")
def comment_save(
    request: Request,
    transfer_id: int,
    body: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Set the transfer's single comment; an empty body removes it."""
    transfer = _get_transfer(db, transfer_id)
    if not can_comment(db, user, transfer):
        raise Forbidden("Only owners of the wallets involved in this transfer can comment.")
    body = body.strip()
    comment = transfer.comment
    if not body:
        if comment:
            db.delete(comment)
            flash(request, "Comment removed.", "success")
    elif comment:
        if comment.body != body:
            comment.body, comment.user_id, comment.created_at = body, user.id, utcnow()
            flash(request, "Comment updated.", "success")
    else:
        db.add(Comment(transfer_id=transfer.id, user_id=user.id, body=body))
        flash(request, "Comment saved.", "success")
    db.commit()
    return redirect(f"/transfers/{transfer_id}#comment")
