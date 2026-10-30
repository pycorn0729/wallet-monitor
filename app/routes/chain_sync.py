"""Which blocks are synced, and scanning a chosen block range from the archive node (admins)."""

from datetime import timezone

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Forbidden, current_user, require_admin
from ..chain import LAST_BLOCK_KEY
from ..db import get_db
from ..models import JOB_ACTIVE, SyncedRange, SyncJob, User, utcnow
from ..services import kv_get, unsynced_span
from ..web import flash, redirect, render

router = APIRouter(prefix="/chain-sync")


def _overview(db: Session) -> dict:
    ranges = db.scalars(select(SyncedRange).order_by(SyncedRange.start_block.desc())).all()
    # Newest first, with the unscanned gap below each range (if another range follows).
    rows = []
    for newer, older in zip(ranges, [*ranges[1:], None]):
        rows.append({"range": newer})
        if older:
            rows.append({"gap": (older.end_block + 1, newer.start_block - 1)})
    live = kv_get(db, LAST_BLOCK_KEY)
    # Newest block with a known time: the form estimates other blocks' times from it (12 s blocks).
    anchor = next((r for r in ranges if r.end_time), None)
    return {
        "rows": rows,
        "range_count": len(ranges),
        "synced_blocks": sum(r.blocks for r in ranges),
        "live_block": int(live) if live is not None else None,
        "jobs": db.scalars(select(SyncJob).order_by(SyncJob.id.desc()).limit(20)).all(),
        "job_active": JOB_ACTIVE,
        "anchor": anchor and (anchor.end_block, int(anchor.end_time.replace(tzinfo=timezone.utc).timestamp() * 1000)),
    }


@router.get("")
def sync_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "chain_sync.html", **_overview(db))


@router.get("/status")
def sync_status(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Just the ranges and jobs; the sync page polls it."""
    return render(request, "_chain_sync.html", **_overview(db))


@router.post("/jobs")
def job_create(
    request: Request,
    start_block: int = Form(...),
    end_block: int = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    live = kv_get(db, LAST_BLOCK_KEY)
    if start_block < 1 or end_block < start_block:
        flash(request, "The start block must be at least 1 and the end block not below it.", "error")
    elif live is not None and start_block > int(live):
        flash(request, f"Blocks after #{int(live):,} are synced by the live watcher as they arrive.", "error")
    elif unsynced_span(db, start_block, end_block)[0] > end_block:
        flash(request, f"Blocks #{start_block:,}–#{end_block:,} are already synced.", "info")
    else:
        db.add(SyncJob(start_block=start_block, end_block=end_block, next_block=start_block, created_by_id=admin.id))
        db.commit()
        flash(request, f"Queued a sync of blocks #{start_block:,}–#{end_block:,}.", "success")
    return redirect("/chain-sync")


@router.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    job = db.get(SyncJob, job_id)
    if not job or job.state not in JOB_ACTIVE:
        raise Forbidden("No queued or running sync with that id.")
    job.state, job.finished_at = "cancelled", utcnow()
    db.commit()
    flash(request, f"Cancelled the sync of blocks #{job.start_block:,}–#{job.end_block:,}.", "success")
    return redirect("/chain-sync")
