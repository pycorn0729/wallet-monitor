"""Which blocks are synced, and scanning a chosen block range from the archive node (admins)."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Forbidden, current_user, require_admin
from ..chain import LAST_BLOCK_KEY, blocks_between
from ..config import get_settings
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
        "today": utcnow().date(),
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
async def job_create(
    request: Request,
    by: str = Form("block"),
    start_block: int | None = Form(None),
    end_block: int | None = Form(None),
    start_date: date | None = Form(None),
    end_date: date | None = Form(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if by == "date":
        if start_date is None or end_date is None or end_date < start_date:
            flash(request, "Pick a From day and a To day on or after it.", "error")
            return redirect("/chain-sync")
        # Whole UTC days, the To day included: its last block is the one before next midnight.
        start, end = datetime.combine(start_date, time()), datetime.combine(end_date + timedelta(days=1), time())
        try:
            start_block, end_block = await asyncio.wait_for(blocks_between(start, end, get_settings().archive_url), 60)
        except Exception as e:
            flash(request, f"Couldn't look up the blocks of those days on the archive node: {str(e) or type(e).__name__}", "error")
            return redirect("/chain-sync")
        if start_block > end_block:
            flash(request, "There are no blocks in those days yet.", "error")
            return redirect("/chain-sync")
    else:
        start_date = end_date = None

    live = kv_get(db, LAST_BLOCK_KEY)
    if start_block is None or end_block is None or start_block < 1 or end_block < start_block:
        flash(request, "The start block must be at least 1 and the end block not below it.", "error")
        return redirect("/chain-sync")
    if live is not None and start_block > int(live):
        flash(request, f"Blocks after #{int(live):,} are synced by the live watcher as they arrive.", "error")
        return redirect("/chain-sync")
    if live is not None:
        end_block = min(end_block, int(live))  # the live watcher syncs the rest
    what = f"blocks #{start_block:,}–#{end_block:,}"
    if start_date:
        what = f"{start_date} – {end_date} (UTC), {what}"
    if unsynced_span(db, start_block, end_block)[0] > end_block:
        flash(request, f"Already synced: {what}.", "info")
    else:
        db.add(
            SyncJob(
                start_block=start_block, end_block=end_block, next_block=start_block,
                start_date=start_date, end_date=end_date, created_by_id=admin.id,
            )
        )
        db.commit()
        flash(request, f"Queued a sync of {what}.", "success")
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
