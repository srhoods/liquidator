"""Worker background jobs: maintenance pause for restores, scheduled backups, retention prune."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from croniter import croniter
from sqlalchemy import func, select

from .. import db, settings_store
from ..backup import service
from ..models import Backup
from .writer import Writer

log = logging.getLogger(__name__)


async def maintenance_watch(writer: Writer) -> None:
    """While `maintenance` is set (a restore is running), stop writing and say so via `worker_paused`."""
    while True:
        try:
            async with db.session_factory()() as s:
                want_pause = bool(await settings_store.get(s, "maintenance", False))
                if want_pause and writer.running.is_set():
                    writer.running.clear()
                    log.info("maintenance: pausing writer")
                    try:
                        await asyncio.wait_for(writer.idle.wait(), 15)
                    except asyncio.TimeoutError:
                        pass
                    await settings_store.put(s, "worker_paused", True)
                elif not want_pause and not writer.running.is_set():
                    writer.running.set()
                    log.info("maintenance over: writer resumed")
                    await settings_store.put(s, "worker_paused", False)
        except Exception:
            log.exception("maintenance poll failed")
        await asyncio.sleep(2)


async def scheduled_jobs() -> None:
    started = datetime.now(timezone.utc)
    last_prune_day = None
    while True:
        await asyncio.sleep(30)
        try:
            async with db.session_factory()() as s:
                sched = await settings_store.get(s, "backup.schedule", {}) or {}
                retention_days = int(await settings_store.get(s, "retention.days", 0) or 0)
                if await settings_store.get(s, "maintenance", False):
                    continue
                last = (await s.execute(select(func.max(Backup.created_at)).where(
                    Backup.kind == "scheduled"))).scalar_one() or started
            if sched.get("enabled") and sched.get("cron"):
                try:
                    due = croniter(sched["cron"], last).get_next(datetime)
                except (ValueError, KeyError):
                    log.warning("invalid backup cron %r", sched.get("cron"))
                    due = None
                if due and due <= datetime.now(timezone.utc):
                    log.info("running scheduled backup")
                    await service.create_backup("scheduled")
            today = datetime.now(timezone.utc).date()
            if retention_days > 0 and last_prune_day != today:
                last_prune_day = today
                n = await service.prune(retention_days)
                log.info("retention: pruned %d events older than %d days", n, retention_days)
        except Exception:
            log.exception("scheduled job failed")
