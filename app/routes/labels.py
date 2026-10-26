from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import ss58
from ..auth import Forbidden, require_admin
from ..db import get_db
from ..models import AddressLabel, User
from ..services import wallet_map
from ..web import flash, redirect, render

router = APIRouter(prefix="/labels")


@router.get("")
def label_list(
    request: Request, q: str = "", address: str = "", admin: User = Depends(require_admin), db: Session = Depends(get_db)
):
    query = select(AddressLabel).order_by(AddressLabel.label)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(AddressLabel.label.ilike(like), AddressLabel.address.ilike(like), AddressLabel.note.ilike(like)))
    return render(
        request,
        "labels.html",
        labels=db.scalars(query).all(),
        q=q,
        prefill=address.strip(),  # "Label this address" links open the dialog prefilled
        wallets=wallet_map(db),
    )


@router.post("")
def label_save(
    request: Request,
    address: str = Form(...),
    label: str = Form(...),
    note: str = Form(""),
    admin: User = Depends(require_admin),
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
        row.label, row.note = label.strip(), note.strip()
        flash(request, f"Label “{row.label}” updated.", "success")
    else:
        db.add(AddressLabel(address=address, label=label.strip(), note=note.strip(), created_by_id=admin.id))
        flash(request, f"Labeled {ss58.short(address)} as “{label.strip()}”.", "success")
    db.commit()
    return redirect(request.headers.get("referer") or "/labels")


@router.post("/{label_id}/delete")
def label_delete(request: Request, label_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    row = db.get(AddressLabel, label_id)
    if not row:
        raise Forbidden("Label not found.")
    db.delete(row)
    db.commit()
    flash(request, f"Label “{row.label}” removed.", "success")
    return redirect("/labels")
