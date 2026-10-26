from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import ss58
from ..auth import Forbidden, current_user
from ..db import get_db
from ..models import KINDS, RAO_PER_TAO, Transfer, User, Wallet
from ..config import get_settings
from ..services import paginate, stake_positions, store_transfers, unapproved_by_address, visible, wallet_map
from ..taostats import TaostatsError
from ..web import flash, redirect, render

router = APIRouter()


def _get_wallet(db: Session, wallet_id: int) -> Wallet:
    wallet = db.get(Wallet, wallet_id)
    if not wallet:
        raise Forbidden("Wallet not found.")
    return wallet


def _check_manage(user: User, wallet: Wallet) -> None:
    if not (user.is_admin or wallet.owner_id == user.id):
        raise Forbidden("Only the wallet owner (or an admin) can change this wallet.")


def _parse_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


@router.get("/wallets")
def wallet_list(
    request: Request,
    owner: str = "",
    min_balance: str = "",
    max_balance: str = "",
    q: str = "",
    sort: str = "total",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    total_expr = Wallet.free_rao + Wallet.staked_rao
    query = select(Wallet)
    if owner.isdigit():
        query = query.where(Wallet.owner_id == int(owner))
    lo, hi = _parse_float(min_balance), _parse_float(max_balance)
    if lo is not None:
        query = query.where(total_expr > int(lo * RAO_PER_TAO))
    if hi is not None:
        query = query.where(total_expr <= int(hi * RAO_PER_TAO))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Wallet.name.ilike(like), Wallet.address.ilike(like), Wallet.purpose.ilike(like)))
    order = {
        "total": total_expr.desc(),
        "name": Wallet.name.asc(),
        "owner": Wallet.owner_id.asc(),
        "created": Wallet.created_at.desc(),
    }.get(sort, total_expr.desc())
    wallets = db.scalars(query.order_by(order)).all()

    return render(
        request,
        "wallets.html",
        wallets=wallets,
        users=db.scalars(select(User).order_by(User.username)).all(),
        unapproved=unapproved_by_address(db),
        filters={"owner": owner, "min_balance": min_balance, "max_balance": max_balance, "q": q, "sort": sort},
        filtered_free=sum(w.free for w in wallets),
        filtered_staked=sum(w.staked for w in wallets),
        filtered_total=sum(w.total for w in wallets),
        taostats_enabled=request.app.state.taostats.enabled,
    )


@router.post("/wallets")
def wallet_create(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    purpose: str = Form(""),
    owner_id: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        address = ss58.normalize(address.strip())
    except ValueError:
        flash(request, "That is not a valid SS58 address.", "error")
        return redirect("/wallets")
    if db.scalars(select(Wallet).where(Wallet.address == address)).first():
        flash(request, "This address is already registered.", "error")
        return redirect("/wallets")

    owner = user.id
    if user.is_admin and owner_id.isdigit() and db.get(User, int(owner_id)):
        owner = int(owner_id)
    wallet = Wallet(name=name.strip() or ss58.short(address), address=address, purpose=purpose.strip(), owner_id=owner)
    db.add(wallet)
    db.commit()
    request.app.state.balances.request_refresh()
    flash(request, f"Wallet “{wallet.name}” added. Balance will refresh shortly.", "success")
    return redirect(f"/wallets/{wallet.id}")


@router.get("/wallets/{wallet_id}")
def wallet_detail(
    request: Request,
    wallet_id: int,
    page: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    wallet = _get_wallet(db, wallet_id)
    pg = paginate(
        db,
        select(Transfer)
        .where(or_(Transfer.from_address == wallet.address, Transfer.to_address == wallet.address), visible())
        .order_by(Transfer.block_number.desc(), Transfer.event_index.desc()),
        page,
    )
    return render(
        request,
        "wallet_detail.html",
        wallet=wallet,
        positions=stake_positions(wallet),
        transfers=pg["rows"],
        pg=pg,
        wallets=wallet_map(db),
        unapproved=unapproved_by_address(db).get(wallet.address, 0),
        can_manage=user.is_admin or wallet.owner_id == user.id,
        users=db.scalars(select(User).order_by(User.username)).all(),
        taostats_enabled=request.app.state.taostats.enabled,
    )


@router.post("/wallets/{wallet_id}/edit")
def wallet_edit(
    request: Request,
    wallet_id: int,
    name: str = Form(...),
    purpose: str = Form(""),
    owner_id: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    wallet = _get_wallet(db, wallet_id)
    _check_manage(user, wallet)
    wallet.name = name.strip() or wallet.name
    wallet.purpose = purpose.strip()
    if user.is_admin and owner_id.isdigit() and db.get(User, int(owner_id)):
        wallet.owner_id = int(owner_id)
    db.commit()
    flash(request, "Wallet updated.", "success")
    return redirect(f"/wallets/{wallet.id}")


@router.post("/wallets/{wallet_id}/delete")
def wallet_delete(
    request: Request, wallet_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    wallet = _get_wallet(db, wallet_id)
    _check_manage(user, wallet)
    db.delete(wallet)
    db.commit()
    flash(request, f"Wallet “{wallet.name}” removed. Its stored transfer history is kept.", "success")
    return redirect("/wallets")


@router.post("/wallets/{wallet_id}/refresh")
def wallet_refresh(
    request: Request, wallet_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _get_wallet(db, wallet_id)
    request.app.state.balances.request_refresh()
    flash(request, "Balance refresh requested.", "info")
    return redirect(f"/wallets/{wallet_id}")


@router.post("/wallets/{wallet_id}/import")
async def wallet_import(
    request: Request, wallet_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    """Backfill past transfers of this wallet from Taostats (the chain watcher only sees new blocks)."""
    wallet = _get_wallet(db, wallet_id)
    _check_manage(user, wallet)
    try:
        rows = await request.app.state.taostats.history(wallet.address, set(KINDS), max_items=1000)
    except TaostatsError as e:
        flash(request, str(e), "error")
        return redirect(f"/wallets/{wallet_id}")
    min_rao = get_settings().min_transfer_rao
    rows = [
        row for row in rows
        if row["amount_rao"] >= min_rao
        and not ss58.is_module_account(row["from_address"])
        and not ss58.is_module_account(row["to_address"])
    ]
    for row in rows:
        row["source"] = "taostats"
    added = store_transfers(db, rows)
    flash(request, f"Imported {added} new transfer(s) from Taostats ({len(rows)} fetched).", "success")
    return redirect(f"/wallets/{wallet_id}")
