from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..chain import sync_summary
from ..db import get_db
from ..models import RAO_PER_TAO, Transfer, User, Wallet
from ..services import related, unapproved_by_address, unapproved_count, visible, wallet_map
from ..web import render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    free, staked, count = db.execute(
        select(func.coalesce(func.sum(Wallet.free_rao), 0), func.coalesce(func.sum(Wallet.staked_rao), 0), func.count(Wallet.id))
    ).one()

    unapproved = unapproved_by_address(db)
    per_owner = []
    for owner in db.scalars(select(User).order_by(User.username)):
        if not owner.wallets:
            continue
        per_owner.append(
            {
                "user": owner,
                "wallets": len(owner.wallets),
                "free": sum(w.free for w in owner.wallets),
                "staked": sum(w.staked for w in owner.wallets),
                "total": sum(w.total for w in owner.wallets),
                "unapproved": unapproved_count(db, [w.address for w in owner.wallets]),
            }
        )

    recent = db.scalars(select(Transfer).where(related(), visible()).order_by(Transfer.block_number.desc(), Transfer.event_index.desc()).limit(10)).all()
    top_wallets = db.scalars(select(Wallet).order_by((Wallet.free_rao + Wallet.staked_rao).desc()).limit(8)).all()
    return render(
        request,
        "dashboard.html",
        total_free=free / RAO_PER_TAO,
        total_staked=staked / RAO_PER_TAO,
        total=(free + staked) / RAO_PER_TAO,
        wallet_count=count,
        per_owner=per_owner,
        recent=recent,
        top_wallets=top_wallets,
        unapproved=unapproved,
        wallets=wallet_map(db),
        sync=sync_summary(request.app.state.watcher, request.app.state.backfill),
        balances=request.app.state.balances.status,
    )


@router.get("/sync")
def sync_card(request: Request, user: User = Depends(current_user)):
    """Just the chain sync card; the dashboard polls it."""
    return render(
        request,
        "_sync.html",
        sync=sync_summary(request.app.state.watcher, request.app.state.backfill),
        balances=request.app.state.balances.status,
    )
