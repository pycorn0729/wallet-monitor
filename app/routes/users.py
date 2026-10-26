from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Forbidden, hash_password, require_admin
from ..db import get_db
from ..models import User
from ..web import flash, redirect, render

router = APIRouter(prefix="/users")


@router.get("")
def user_list(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.username)).all()
    pending = sorted((u for u in users if not u.is_approved), key=lambda u: u.created_at)
    return render(request, "users.html", users=[u for u in users if u.is_approved], pending=pending)


@router.post("/{user_id}/approve")
def user_approve(
    request: Request,
    user_id: int,
    is_approver: bool = Form(False),
    is_admin: bool = Form(False),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target or target.is_approved:
        raise Forbidden("No pending registration with that id.")
    target.is_approved = True
    target.is_active = True
    target.is_approver, target.is_admin = is_approver, is_admin
    db.commit()
    flash(request, f"Approved “{target.username}”. They can sign in now.", "success")
    return redirect("/users")


@router.post("/{user_id}/reject")
def user_reject(request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target or target.is_approved:
        raise Forbidden("No pending registration with that id.")
    db.delete(target)
    db.commit()
    flash(request, f"Rejected registration of “{target.username}”.", "success")
    return redirect("/users")


@router.post("/{user_id}")
def user_update(
    request: Request,
    user_id: int,
    is_admin: bool = Form(False),
    is_approver: bool = Form(False),
    is_active: bool = Form(False),
    password: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if not target:
        raise Forbidden("User not found.")
    if target.id == admin.id and (not is_admin or not is_active):
        flash(request, "You cannot remove your own admin rights or deactivate yourself.", "error")
        return redirect("/users")
    target.is_admin, target.is_approver, target.is_active = is_admin, is_approver, is_active
    if password:
        if len(password) < 8:
            flash(request, "Password must be at least 8 characters.", "error")
            return redirect("/users")
        target.password_hash = hash_password(password)
    db.commit()
    flash(request, f"User “{target.username}” updated.", "success")
    return redirect("/users")


@router.post("/{user_id}/delete")
def user_delete(request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise Forbidden("User not found.")
    if target.id == admin.id:
        flash(request, "You cannot delete yourself.", "error")
    elif target.wallets:
        flash(request, "Reassign or remove this user's wallets first (or just deactivate the user).", "error")
    else:
        db.delete(target)
        db.commit()
        flash(request, f"User “{target.username}” deleted.", "success")
    return redirect("/users")
