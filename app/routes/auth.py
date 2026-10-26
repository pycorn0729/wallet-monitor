import re

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import authenticate, current_user, hash_password, verify_password
from ..db import get_db
from ..models import User
from ..web import flash, redirect, render

router = APIRouter()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


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
    if not user.is_approved:
        return render(request, "login.html", next=next, error="Your account is waiting for an admin to approve it.")
    if not user.is_active:
        return render(request, "login.html", next=next, error="Your account has been deactivated.")
    request.session.clear()
    request.session["uid"] = user.id
    return redirect(_safe_next(next))


@router.get("/register")
def register_page(request: Request):
    if request.session.get("uid"):
        return redirect("/")
    return render(request, "register.html")


@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    error = None
    if not USERNAME_RE.match(username):
        error = "Username must be 3–64 characters: letters, digits, “_”, “.” or “-”."
    elif len(password) < 8 or password != confirm:
        error = "Password must be at least 8 characters and match the confirmation."
    elif db.scalars(select(User).where(User.username == username)).first():
        error = "That username is taken."
    if error:
        return render(request, "register.html", error=error, username=username)
    db.add(User(username=username, password_hash=hash_password(password), is_approved=False))
    db.commit()
    return render(request, "login.html", next="/", notice="Account created. An admin must approve it before you can sign in.")


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
