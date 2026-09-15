"""Automatic retention cleanup.

Expiry counts from the moment a job reached a terminal state (failed and
canceled jobs expire too). Cleanup runs at startup and periodically while
running; jobs that expired while the application was closed are removed at
the next startup. Active jobs are never expired. Model downloads and LLM
presets are never part of job retention.
"""
import asyncio
import logging
import shutil
import time
from typing import Any, Dict, List

from . import config, db
from .events import bus
from .jobs import job_dir

log = logging.getLogger("retention")

CLEANUP_INTERVAL_S = 3600

ACTIVE_STATUSES = ("queued", "running", "recording")


def cleanup(now: float = None) -> List[str]:
    removed = []
    for job in db.expired_jobs(now):
        if job["status"] in ACTIVE_STATUSES:
            continue  # active jobs are excluded from expiry
        d = job_dir(job["id"])
        db.delete_job_rows(job["id"])
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        removed.append(job["id"])
        bus.publish_threadsafe({"type": "job.deleted", "job_id": job["id"],
                                "reason": "expired"})
    if removed:
        log.info("retention removed %d expired job(s)", len(removed))
    return removed


def apply_retention_change(days: float) -> None:
    """Recompute expiry for existing inactive terminal jobs from their
    terminal timestamps; future jobs pick the new value up automatically."""
    db.set_setting("retention_days", days)
    for job in db.list_jobs():
        if job.get("terminal_at") and job["status"] not in ACTIVE_STATUSES:
            expires = job["terminal_at"] + days * 86400 if days > 0 else None
            db.update_job(job["id"], expires_at=expires)
    cleanup()


def preview_retention_change(days: float) -> List[Dict[str, Any]]:
    """Jobs that would expire immediately under the shorter period."""
    now = time.time()
    affected = []
    for job in db.list_jobs():
        if job.get("terminal_at") and job["status"] not in ACTIVE_STATUSES:
            if job["terminal_at"] + days * 86400 <= now:
                affected.append({"id": job["id"], "display_name": job["display_name"],
                                 "terminal_at": job["terminal_at"]})
    return affected


async def retention_loop() -> None:
    while True:
        try:
            cleanup()
        except Exception:
            log.exception("retention cleanup failed")
        await asyncio.sleep(CLEANUP_INTERVAL_S)
