from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import ss58
from ..auth import Forbidden, current_user
from ..db import get_db
from ..models import AddressLabel, User
from ..services import paginate, wallet_map
from ..web import flash, redirect, render

router = APIRouter(prefix="/labels")


def can_edit(user: User, row: AddressLabel) -> bool:
    """Anyone can add labels; a label can be changed or removed by its author or an admin."""
    return user.is_admin or row.created_by_id == user.id


@router.get("")
def label_list(
    request: Request,
    q: str = "",
    address: str = "",
    page: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    query = select(AddressLabel).order_by(AddressLabel.label, AddressLabel.id)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(AddressLabel.label.ilike(like), AddressLabel.address.ilike(like), AddressLabel.note.ilike(like)))
    pg = paginate(db, query, page, 100)
    return render(
        request,
        "labels.html",
        labels=pg["rows"],
        pg=pg,
        q=q,
        prefill=address.strip(),  # "Label this address" links open the dialog prefilled
        wallets=wallet_map(db),
        can_edit=can_edit,
    )


@router.post("")
def label_save(
    request: Request,
    address: str = Form(...),
    label: str = Form(...),
    note: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Create a label, or update the existing one for that address."""
    try:
        address = ss58.normalize(address.strip())
    except ValueError:
        flash(request, "That is not a valid SS58 address.", "error")
        return redirect("/labels")
    if not label.strip():
        flash(request, "Label text is required.", "error")
        return redirect("/labels")
    row = db.scalars(select(AddressLabel).where(AddressLabel.address == address)).first()
    if row:
        if not can_edit(user, row):
            author = row.created_by.username if row.created_by else "another user"
            flash(request, f"{ss58.short(address)} is already labeled “{row.label}” by {author}; only they or an admin can change it.", "error")
            return redirect(request.headers.get("referer") or "/labels")
        row.label, row.note = label.strip(), note.strip()
        flash(request, f"Label “{row.label}” updated.", "success")
    else:
        db.add(AddressLabel(address=address, label=label.strip(), note=note.strip(), created_by_id=user.id))
        flash(request, f"Labeled {ss58.short(address)} as “{label.strip()}”.", "success")
    db.commit()
    return redirect(request.headers.get("referer") or "/labels")


@router.post("/{label_id}/delete")
def label_delete(request: Request, label_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(AddressLabel, label_id)
    if not row:
        raise Forbidden("Label not found.")
    if not can_edit(user, row):
        raise Forbidden("Only the label's author or an admin can delete it.")
    db.delete(row)
    db.commit()
    flash(request, f"Label “{row.label}” removed.", "success")
    return redirect("/labels")
