from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..auth import authenticate, current_user, hash_password, verify_password
from ..db import get_db
from ..models import User
from ..web import flash, redirect, render

router = APIRouter()


def _safe_next(url: str | None) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if request.session.get("uid"):
        return redirect(_safe_next(next))
    return render(request, "login.html", next=next)


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = authenticate(db, username.strip(), password)
    if not user:
        return render(request, "login.html", next=next, error="Invalid username or password.")
    request.session.clear()
    request.session["uid"] = user.id
    return redirect(_safe_next(next))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@router.get("/account")
def account_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account.html")


@router.post("/account/password")
def change_password(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    confirm: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(current, user.password_hash):
        flash(request, "Current password is wrong.", "error")
    elif len(new) < 8 or new != confirm:
        flash(request, "New password must be at least 8 characters and match the confirmation.", "error")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        flash(request, "Password changed.", "success")
    return redirect("/account")
