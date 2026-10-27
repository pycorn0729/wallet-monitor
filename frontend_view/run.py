"""Run the wallet monitor with this folder's front end (no transfer approval UI).

The backend in app/ is used as is; only the templates and static files are swapped.
Start from the project root with chain tasks off, since the main instance runs them:

    WM_WATCHER_ENABLED=false .venv/bin/uvicorn frontend_view.run:app --host 127.0.0.1 --port 8001
"""

from pathlib import Path

from jinja2 import FileSystemLoader
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app import web
from app.main import app

HERE = Path(__file__).parent

web.templates.env.loader = FileSystemLoader(str(HERE / "templates"))
app.router.routes = [
    Mount("/static", app=StaticFiles(directory=HERE / "static"), name="static")
    if getattr(route, "name", None) == "static"
    else route
    for route in app.router.routes
]
