import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import Forbidden, NotAuthenticated
from .chain import BalanceRefresher, ChainWatcher, HistoryBackfill, RangeSync, seed_synced_ranges
from .config import get_settings
from .db import init_db
from .routes import auth, chain_sync, dashboard, graph, labels, transfers, users, wallets
from .taostats import TaostatsClient
from .web import flash, redirect, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.taostats = TaostatsClient(settings)
    app.state.watcher = ChainWatcher(settings)
    app.state.balances = BalanceRefresher(settings)
    app.state.backfill = HistoryBackfill(settings)
    tasks = []
    if settings.watcher_enabled:
        seed_synced_ranges()
        tasks = [
            asyncio.create_task(app.state.watcher.run(), name="chain-watcher"),
            asyncio.create_task(app.state.balances.run(), name="balance-refresher"),
            asyncio.create_task(app.state.backfill.run(), name="history-backfill"),
            asyncio.create_task(RangeSync(settings).run(), name="range-sync"),
        ]
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Bittensor Wallet Monitor", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax", max_age=7 * 24 * 3600)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

for module in (auth, dashboard, wallets, transfers, graph, labels, users, chain_sync):
    app.include_router(module.router)


@app.exception_handler(NotAuthenticated)
async def _not_authenticated(request: Request, exc: NotAuthenticated):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return redirect(f"/login?next={request.url.path}")


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.message}, status_code=403)
    if request.method == "GET":
        return render(request, "error.html", status_code=403, message=exc.message)
    flash(request, exc.message, "error")
    return redirect(request.headers.get("referer") or "/")
