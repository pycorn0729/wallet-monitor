from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ss58
from ..auth import current_user
from ..db import get_db
from ..graph import build_graph, ensure_kinds
from ..models import User, Wallet
from ..taostats import TaostatsError
from ..web import render

router = APIRouter()


@router.get("/graph")
def graph_page(request: Request, address: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    taostats_enabled = request.app.state.taostats.enabled
    return render(
        request,
        "graph.html",
        wallets=db.scalars(select(Wallet).order_by(Wallet.name)).all(),
        address=address,
        default_source="taostats" if taostats_enabled and address else "db",
        taostats_enabled=taostats_enabled,
    )


@router.get("/api/graph")
async def graph_data(
    request: Request,
    address: str = "",
    source: str = "db",
    kinds: str = "",
    limit: int = 200,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    addr = None
    if address.strip():
        try:
            addr = ss58.normalize(address.strip())
        except ValueError:
            return JSONResponse({"detail": "Invalid SS58 address."}, status_code=400)
    try:
        data = await build_graph(
            db,
            request.app.state.taostats,
            addr,
            "taostats" if source == "taostats" else "db",
            ensure_kinds(kinds),
            max(1, min(limit, 1000)),
        )
    except (TaostatsError, ValueError) as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return data
