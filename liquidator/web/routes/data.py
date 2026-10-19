from __future__ import annotations

import asyncio
import html
import logging
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import config, crypto as secrets_crypto, settings_store
from ...backup import crypto as bcrypto, remotes, service
from ...models import Backup, BackupTarget
from ..deps import Ctx, admin, admin_post, audit, get_db, render

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/data")

# ---- background jobs (in-memory; the web app is a single process) -----------------------------
JOBS: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()


def start_job(title: str, fn) -> str:
    if any(j["state"] == "running" for j in JOBS.values()):
        raise HTTPException(409, "another backup/restore job is still running")
    jid = secrets.token_urlsafe(8)
    job = {"title": title, "state": "running", "log": [], "error": None,
           "started": datetime.now(timezone.utc)}
    JOBS[jid] = job
    for old in list(JOBS)[:-10]:
        JOBS.pop(old, None)

    async def run() -> None:
        try:
            await fn(lambda m: job["log"].append(m))
            job["state"] = "done"
        except Exception as exc:
            log.exception("job %s failed", title)
            job["state"], job["error"] = "failed", str(exc)[:1000]

    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return jid


def _job_html(job: dict, jid: str) -> str:
    cls = {"running": "", "done": "ok", "failed": "err"}[job["state"]]
    poll = f' hx-get="/admin/data/jobs/{jid}" hx-trigger="every 2s" hx-swap="outerHTML"' if job["state"] == "running" else ""
    steps = "".join(f"<li>{html.escape(m)}</li>" for m in job["log"])
    err = f'<div class="err">{html.escape(job["error"])}</div>' if job["error"] else ""
    state = {"running": "in progress…", "done": "finished", "failed": "FAILED"}[job["state"]]
    return (f'<div class="card" style="margin-bottom:12px"{poll}><strong class="{cls}">{html.escape(job["title"])}: '
            f'{state}</strong><ul class="muted" style="margin:6px 0 0 18px">{steps}</ul>{err}</div>')


@router.get("/jobs/{jid}")
async def job_status(jid: str, ctx: Ctx = Depends(admin)):
    job = JOBS.get(jid)
    if not job:
        return HTMLResponse("")
    return HTMLResponse(_job_html(job, jid))


# ---- main page ---------------------------------------------------------------------------------
async def _page(request: Request, ctx: Ctx, s: AsyncSession, error: str | None = None, status: int = 200):
    backups = await service.sync_local()
    tgs = list((await s.execute(select(BackupTarget).order_by(BackupTarget.id))).scalars())
    stats = await service.datastore_stats()
    jid = request.query_params.get("job")
    return render(
        request, "data.html", ctx, status_code=status, page="data", backups=backups, targets=tgs,
        tname={t.id: t.name for t in tgs}, stats=stats, error=error,
        job_html=_job_html(JOBS[jid], jid) if jid in JOBS else "",
        schedule=await settings_store.get(s, "backup.schedule", {}) or {},
        keep_local=await settings_store.get(s, "backup.keep_local", 14),
        retention=await settings_store.get(s, "retention.days", 0),
        public_key=bcrypto.public_key(), now=datetime.now(timezone.utc))


@router.get("")
async def data_page(request: Request, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    return await _page(request, ctx, s)


@router.post("/backup")
async def backup_now(ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    jid = start_job("Backup", lambda p: service.create_backup("manual", p))
    await audit(s, ctx, "backup.create", "manual")
    return RedirectResponse(f"/admin/data?job={jid}", status_code=303)


def _name(name: str) -> str:
    try:
        service.local_path(name)
    except ValueError:
        raise HTTPException(400, "invalid backup name")
    return name


@router.get("/backups/{name}/download")
async def download(name: str, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    p = service.local_path(_name(name))
    if not p.exists():
        raise HTTPException(404, "no such backup")
    await audit(s, ctx, "backup.download", name)
    return FileResponse(p, media_type="application/octet-stream", filename=name)


@router.post("/backups/upload")
async def upload(request: Request, file: UploadFile = File(...), ctx: Ctx = Depends(admin_post),
                 s: AsyncSession = Depends(get_db)):
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    head = await file.read(64)
    if not bcrypto.looks_like_age(head):
        return await _page(request, ctx, s, "That file is not an age-encrypted backup.", 400)
    await file.seek(0)
    name = file.filename or ""
    if not service.NAME_RE.match(name) or service.local_path(name).exists():
        name = f"liquidator-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-up.dump.age"
    tmp = Path(tempfile.mkstemp(dir=config.BACKUP_DIR, suffix=".part")[1])
    try:
        with open(tmp, "wb") as out:
            await asyncio.to_thread(shutil.copyfileobj, file.file, out, 1 << 20)
        await asyncio.to_thread(service.verify_file, tmp)  # right key, valid pg dump
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return await _page(request, ctx, s, f"Upload rejected: {exc}", 400)
    tmp.chmod(0o600)
    tmp.replace(service.local_path(name))
    await service.sync_local()
    await audit(s, ctx, "backup.upload", name)
    return RedirectResponse("/admin/data", status_code=303)


@router.post("/backups/{name}/delete")
async def delete_backup(name: str, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    service.local_path(_name(name)).unlink(missing_ok=True)
    await service.sync_local()
    await audit(s, ctx, "backup.delete", name)
    return RedirectResponse("/admin/data", status_code=303)


@router.post("/backups/{name}/restore")
async def restore_backup(name: str, request: Request, confirm: str = Form(""),
                         ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    _name(name)
    if confirm.strip() != name:
        return await _page(request, ctx, s, "Type the exact backup file name to confirm the restore.", 400)
    jid = start_job(f"Restore {name}", lambda p: service.restore(name, p))
    await audit(s, ctx, "backup.restore", name)
    return RedirectResponse(f"/admin/data?job={jid}", status_code=303)


@router.post("/backup-key")
async def backup_key(ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    await audit(s, ctx, "backup.key_download", "")
    return PlainTextResponse(
        f"# Liquidator backup key. Anyone with this key can read your backups. Store it safely.\n"
        f"# public key: {bcrypto.public_key()}\n{config.backup_key()}\n",
        headers={"Content-Disposition": 'attachment; filename="liquidator-backup.key"'})


@router.post("/schedule")
async def save_schedule(request: Request, enabled: str = Form("off"), cron: str = Form("0 3 * * *"),
                        keep_local: int = Form(14), ctx: Ctx = Depends(admin_post),
                        s: AsyncSession = Depends(get_db)):
    cron = cron.strip()
    if not croniter.is_valid(cron):
        return await _page(request, ctx, s, "That is not a valid 5-field cron expression.", 400)
    await settings_store.put(s, "backup.schedule", {"enabled": enabled == "on", "cron": cron})
    await settings_store.put(s, "backup.keep_local", max(1, min(1000, keep_local)))
    await audit(s, ctx, "backup.schedule", f"enabled={enabled} cron={cron} keep={keep_local}")
    return RedirectResponse("/admin/data", status_code=303)


@router.post("/retention")
async def save_retention(days: int = Form(0), ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    days = max(0, days)
    await settings_store.put(s, "retention.days", days)
    await audit(s, ctx, "retention.set", f"{days} days (0 = keep forever)")
    return RedirectResponse("/admin/data", status_code=303)


@router.post("/vacuum")
async def do_vacuum(ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    await service.vacuum()
    await audit(s, ctx, "datastore.vacuum", "")
    return RedirectResponse("/admin/data", status_code=303)


# ---- remote targets ----------------------------------------------------------------------------
SECRET_KEYS = {"secret_key", "password", "private_key", "key_passphrase"}
TARGET_FIELDS = {"s3": ["endpoint_url", "region", "bucket", "prefix", "access_key", "secret_key"],
                 "sftp": ["host", "port", "username", "path", "password", "private_key", "key_passphrase"]}


def _tview(t: BackupTarget | None, ttype: str = "s3") -> dict:
    if not t:
        return {"id": None, "name": "", "type": ttype, "enabled": True, "keep": 14, "cfg": {}, "secrets_set": {}}
    cfg = secrets_crypto.decrypt_json(t.config_enc)
    return {"id": t.id, "name": t.name, "type": t.type, "enabled": t.enabled, "keep": t.keep,
            "cfg": {k: v for k, v in cfg.items() if k not in SECRET_KEYS and k not in ("host_key", "host_fingerprint")},
            "secrets_set": {k: bool(cfg.get(k)) for k in SECRET_KEYS},
            "fingerprint": cfg.get("host_fingerprint", "")}


async def _tparse(request: Request, ttype: str, existing: dict | None = None):
    form = await request.form()
    cfg = dict(existing or {})
    for k in TARGET_FIELDS[ttype]:
        v = (form.get(k) or "").strip()
        if v or k not in SECRET_KEYS:
            cfg[k] = v
    return (form.get("name") or "").strip()[:100], form.get("enabled") == "on", \
        max(1, min(1000, int(form.get("keep") or 14))), cfg


async def _pin_sftp(cfg: dict, old: dict | None) -> str | None:
    """Pin the SFTP host key on first save (or when host/port changes); returns an error or None."""
    if old and old.get("host_key") and old.get("host") == cfg.get("host") and old.get("port") == cfg.get("port"):
        cfg["host_key"] = old["host_key"]
        cfg["host_fingerprint"] = old.get("host_fingerprint", "")
        return None
    try:
        key, fp = await remotes.fetch_sftp_host_key(cfg["host"], int(cfg.get("port") or 22))
    except Exception as exc:
        return f"Could not read the SFTP server's host key: {exc}"
    cfg["host_key"], cfg["host_fingerprint"] = key, fp
    return None


@router.get("/targets/new")
async def new_target(request: Request, type: str = "s3", ctx: Ctx = Depends(admin)):
    return render(request, "target_form.html", ctx, page="data", t=_tview(None, type if type in ("s3", "sftp") else "s3"),
                  error=None)


@router.post("/targets")
async def create_target(request: Request, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    ttype = (await request.form()).get("type")
    if ttype not in TARGET_FIELDS:
        raise HTTPException(400, "bad type")
    name, enabled, keep, cfg = await _tparse(request, ttype)
    err = None if name else "Name is required."
    err = err or remotes.validate(ttype, cfg)
    if not err and ttype == "sftp":
        err = await _pin_sftp(cfg, None)
    if err:
        view = _tview(None, ttype)
        view.update(name=name, enabled=enabled, keep=keep, cfg={k: v for k, v in cfg.items() if k not in SECRET_KEYS})
        return render(request, "target_form.html", ctx, status_code=400, page="data", t=view, error=err)
    t = BackupTarget(name=name, type=ttype, enabled=enabled, keep=keep, config_enc=secrets_crypto.encrypt_json(cfg))
    s.add(t)
    await s.commit()
    await audit(s, ctx, "target.create", f"{name} ({ttype})")
    return RedirectResponse(f"/admin/data/targets/{t.id}", status_code=303)


@router.get("/targets/{tid}")
async def edit_target(tid: int, request: Request, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    t = await s.get(BackupTarget, tid)
    if not t:
        raise HTTPException(404, "no such target")
    return render(request, "target_form.html", ctx, page="data", t=_tview(t), error=None)


@router.post("/targets/{tid}")
async def update_target(tid: int, request: Request, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    t = await s.get(BackupTarget, tid)
    if not t:
        raise HTTPException(404, "no such target")
    old = secrets_crypto.decrypt_json(t.config_enc)
    name, enabled, keep, cfg = await _tparse(request, t.type, old)
    err = None if name else "Name is required."
    err = err or remotes.validate(t.type, cfg)
    if not err and t.type == "sftp":
        err = await _pin_sftp(cfg, old)
    if err:
        view = _tview(t)
        view.update(name=name, enabled=enabled, keep=keep)
        return render(request, "target_form.html", ctx, status_code=400, page="data", t=view, error=err)
    t.name, t.enabled, t.keep, t.config_enc = name, enabled, keep, secrets_crypto.encrypt_json(cfg)
    await s.commit()
    await audit(s, ctx, "target.update", name)
    return RedirectResponse(f"/admin/data/targets/{tid}", status_code=303)


@router.post("/targets/{tid}/delete")
async def delete_target(tid: int, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    t = await s.get(BackupTarget, tid)
    if not t:
        raise HTTPException(404, "no such target")
    await s.delete(t)
    await s.commit()
    await audit(s, ctx, "target.delete", t.name)
    return RedirectResponse("/admin/data", status_code=303)


async def _remote(tid: int, s: AsyncSession):
    t = await s.get(BackupTarget, tid)
    if not t:
        raise HTTPException(404, "no such target")
    return t, remotes.make_remote(t.type, secrets_crypto.decrypt_json(t.config_enc))


@router.post("/targets/{tid}/test")
async def test_target(tid: int, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    t, r = await _remote(tid, s)
    try:
        res, cls = await asyncio.wait_for(r.test(), 30), "ok"
    except Exception as exc:
        res, cls = f"Failed: {type(exc).__name__}: {exc}", "err"
    await audit(s, ctx, "target.test", f"{t.name}: {res}")
    return HTMLResponse(f'<span class="{cls}">{html.escape(str(res))}</span>')


@router.get("/targets/{tid}/files")
async def target_files(tid: int, request: Request, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    t, r = await _remote(tid, s)
    error, files = None, []
    try:
        files = sorted(await asyncio.wait_for(r.list(), 60), key=lambda f: f["name"], reverse=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    local = {b.filename for b in await service.sync_local()}
    return render(request, "target_files.html", ctx, page="data", t=t, files=files, error=error, local=local)


@router.post("/targets/{tid}/files/{name}/fetch")
async def fetch_file(tid: int, name: str, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    t, r = await _remote(tid, s)
    dest = service.local_path(_name(name))
    if dest.exists():
        raise HTTPException(409, "already present locally")

    async def job(progress):
        progress(f"downloading {name} from {t.name}")
        part = dest.with_suffix(".part")
        try:
            await r.download(name, part)
            progress("verifying (decrypt + parse)")
            await asyncio.to_thread(service.verify_file, part)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        part.chmod(0o600)
        part.replace(dest)
        await service.sync_local()

    jid = start_job(f"Fetch {name}", job)
    await audit(s, ctx, "target.fetch", f"{name} from {t.name}")
    return RedirectResponse(f"/admin/data?job={jid}", status_code=303)


@router.post("/targets/{tid}/files/{name}/delete")
async def delete_remote_file(tid: int, name: str, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    t, r = await _remote(tid, s)
    await r.delete(_name(name))
    await audit(s, ctx, "target.delete_file", f"{name} on {t.name}")
    return RedirectResponse(f"/admin/data/targets/{tid}/files", status_code=303)
