"""Template setup and small request helpers shared by the route modules."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import ss58
from .db import SessionLocal
from .models import KINDS
from .services import unapproved_count

templates_dir = Path(__file__).parent / "templates"


def _context(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    ctx = {"user": user, "flashes": request.session.pop("flashes", []) if "session" in request.scope else []}
    if user:
        with SessionLocal() as db:
            ctx["nav_unapproved"] = unapproved_count(db)
    return ctx


templates = Jinja2Templates(directory=str(templates_dir), context_processors=[_context])


def fmt_tao(value, digits: int = 4) -> str:
    if value is None:
        return "–"
    return f"{value:,.{digits}f}"


def fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else "–"


templates.env.filters["tao"] = fmt_tao
templates.env.filters["dt"] = fmt_dt
templates.env.filters["short"] = ss58.short
templates.env.globals["KINDS"] = KINDS


def flash(request: Request, message: str, category: str = "info") -> None:
    request.session.setdefault("flashes", []).append({"message": message, "category": category})


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request, name, context, status_code=status_code)
