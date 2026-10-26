from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ss58
from ..auth import current_user
from ..db import get_db
from ..graph import build_graph, ensure_kinds
from ..models import Transfer, User, Wallet
from ..web import render

router = APIRouter()


@router.get("/graph")
def graph_page(request: Request, address: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    oldest_block, oldest_time, newest_block = db.execute(
        select(func.min(Transfer.block_number), func.min(Transfer.timestamp), func.max(Transfer.block_number))
    ).one()
    return render(
        request,
        "graph.html",
        wallets=db.scalars(select(Wallet).order_by(Wallet.name)).all(),
        address=address,
        coverage={"oldest_block": oldest_block, "oldest_time": oldest_time, "newest_block": newest_block},
        backfill=request.app.state.backfill,
    )


@router.get("/api/graph")
def graph_data(
    address: str = "",
    kinds: str = "",
    limit: int = 300,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not address.strip():
        return JSONResponse({"detail": "Pick a wallet or enter an address."}, status_code=400)
    try:
        addr = ss58.normalize(address.strip())
    except ValueError:
        return JSONResponse({"detail": "Invalid SS58 address."}, status_code=400)
    return build_graph(db, addr, ensure_kinds(kinds), max(1, min(limit, 2000)))
