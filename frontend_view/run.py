"""Run the wallet monitor with this folder's front end (no transfer approval UI).

The backend in app/ is used as is; only the templates and static files are swapped.
Start from the project root with chain tasks off, since the main instance runs them:

    WM_WATCHER_ENABLED=false .venv/bin/uvicorn frontend_view.run:app --host 127.0.0.1 --port 8001

Only a wallet's owner can edit, remove or import history for it; unlike the main site,
admins can't change other people's wallets.
"""

from pathlib import Path

from jinja2 import FileSystemLoader
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app import web
from app.auth import Forbidden
from app.main import app
from app.models import User, Wallet
from app.routes import wallets as wallet_routes

HERE = Path(__file__).parent


def _owner_only(user: User, wallet: Wallet) -> None:
    if wallet.owner_id != user.id:
        raise Forbidden("Only the wallet's owner can change this wallet.")


# The wallet routes look this check up at call time, so this covers edit, remove and import.
wallet_routes._check_manage = _owner_only

web.templates.env.loader = FileSystemLoader(str(HERE / "templates"))
app.router.routes = [
    Mount("/static", app=StaticFiles(directory=HERE / "static"), name="static")
    if getattr(route, "name", None) == "static"
    else route
    for route in app.router.routes
]
