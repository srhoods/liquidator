"""Create, verify, restore and prune encrypted pg_dump backups."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from sqlalchemy import delete, select, text

from .. import config, crypto as secrets_crypto, db, settings_store
from ..models import Backup, BackupTarget
from . import crypto
from .remotes import make_remote

log = logging.getLogger(__name__)
NAME_RE = re.compile(r"^liquidator-\d{8}-\d{6}(-[a-z]+)?\.dump\.age$")
Progress = Callable[[str], None]


def pg_env() -> dict:
    return {**os.environ, "PGHOST": config.DB_HOST, "PGUSER": config.DB_USER,
            "PGDATABASE": config.DB_NAME, "PGPASSWORD": config.db_password()}


def local_path(name: str) -> Path:
    if not NAME_RE.match(name):
        raise ValueError("invalid backup name")
    return config.BACKUP_DIR / name


def _tail(f) -> str:
    f.seek(0)
    return f.read().decode(errors="replace").strip()[-800:]


def _dump_encrypt(dest: Path) -> tuple[str, int]:
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(["pg_dump", "-Fc", "--no-owner", "--no-acl"], stdout=subprocess.PIPE,
                                stderr=err, env=pg_env())
        try:
            with open(part, "wb") as f:
                w = crypto.HashingWriter(f)
                crypto.encrypt_stream(proc.stdout, w)
        except Exception:
            proc.kill()
            part.unlink(missing_ok=True)
            raise
        finally:
            proc.stdout.close()
        if proc.wait() != 0:
            part.unlink(missing_ok=True)
            raise RuntimeError("pg_dump failed: " + _tail(err))
    os.chmod(part, 0o600)
    os.replace(part, dest)
    return w.sha.hexdigest(), w.size


def _pipe_decrypted(path: Path, cmd: list[str]) -> None:
    """Decrypt `path` into the stdin of `cmd`; raises with the command's stderr on failure."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err, stdout=subprocess.DEVNULL,
                                env=pg_env())
        dec_err: Optional[Exception] = None
        try:
            with open(path, "rb") as f:
                crypto.decrypt_stream(f, proc.stdin)
        except Exception as exc:
            dec_err = exc
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        rc = proc.wait()
        if dec_err:
            raise RuntimeError(f"could not decrypt backup (wrong key or corrupt file): {dec_err}")
        if rc != 0:
            raise RuntimeError(f"{cmd[0]} failed: " + _tail(err))


def verify_file(path: Path) -> None:
    """Decrypt fully and ask pg_restore to parse the archive, without touching the database."""
    _pipe_decrypted(path, ["pg_restore", "--list"])


def _admin_conn():
    import psycopg2
    conn = psycopg2.connect(host=config.DB_HOST, user=config.DB_USER, password=config.db_password(),
                            dbname="postgres", connect_timeout=10)
    conn.autocommit = True
    return conn


def _restore_swap(path: Path) -> None:
    """Restore into a scratch database first, so a bad or incompatible backup can never damage the
    live data, then swap it in by renaming. Clients are disconnected only for the instant of the swap."""
    live, scratch, old = config.DB_NAME, f"{config.DB_NAME}_restore", f"{config.DB_NAME}_old"
    conn = _admin_conn()
    cur = conn.cursor()
    try:
        for d in (scratch, old):
            cur.execute(f'DROP DATABASE IF EXISTS "{d}"')
        cur.execute(f'CREATE DATABASE "{scratch}"')
        env_db = scratch
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                ["pg_restore", "--no-owner", "--no-acl", "--exit-on-error", "-d", env_db],
                stdin=subprocess.PIPE, stderr=err, stdout=subprocess.DEVNULL, env=pg_env())
            dec_err = None
            try:
                with open(path, "rb") as f:
                    crypto.decrypt_stream(f, proc.stdin)
            except Exception as exc:
                dec_err = exc
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            rc = proc.wait()
            if dec_err:
                raise RuntimeError(f"could not decrypt backup: {dec_err}")
            if rc != 0:
                raise RuntimeError("pg_restore failed: " + _tail(err))
        # swap
        cur.execute(f'ALTER DATABASE "{live}" ALLOW_CONNECTIONS false')
        try:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (live,))
            cur.execute(f'ALTER DATABASE "{live}" RENAME TO "{old}"')
            try:
                cur.execute(f'ALTER DATABASE "{scratch}" RENAME TO "{live}"')
            except Exception:
                cur.execute(f'ALTER DATABASE "{old}" RENAME TO "{live}"')
                raise
        except Exception:
            cur.execute(f'ALTER DATABASE "{live}" ALLOW_CONNECTIONS true')
            raise
        cur.execute(f'DROP DATABASE IF EXISTS "{old}"')
        scratch = None
    finally:
        if scratch:
            try:
                cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
            except Exception:
                log.exception("could not drop scratch database")
        conn.close()


async def sync_local() -> list[Backup]:
    """Make the `backups` table match the files on disk (files are the source of truth;
    a restore can roll the table back to an older state)."""
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = {p.name: p for p in config.BACKUP_DIR.glob("liquidator-*.dump.age") if NAME_RE.match(p.name)}
    async with db.session_factory()() as s:
        rows = {b.filename: b for b in (await s.execute(select(Backup))).scalars()}
        for name, p in files.items():
            if name not in rows:
                st = p.stat()
                kind = "pre-restore" if "-pre" in name else "imported"
                s.add(Backup(filename=name, size_bytes=st.st_size, kind=kind, remotes=[],
                             created_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)))
            else:
                rows[name].size_bytes = files[name].stat().st_size
        for name, b in rows.items():
            if name not in files:
                await s.delete(b)
        await s.commit()
        return list((await s.execute(select(Backup).order_by(Backup.created_at.desc()))).scalars())


async def targets(enabled_only: bool = True) -> list[BackupTarget]:
    async with db.session_factory()() as s:
        q = select(BackupTarget).order_by(BackupTarget.id)
        if enabled_only:
            q = q.where(BackupTarget.enabled)
        return list((await s.execute(q)).scalars())


async def create_backup(kind: str = "manual", progress: Progress = lambda m: None) -> Backup:
    now = datetime.now(timezone.utc)
    suffix = "-pre" if kind == "pre-restore" else ""
    name = f"liquidator-{now:%Y%m%d-%H%M%S}{suffix}.dump.age"
    progress("dumping and encrypting")
    sha, size = await asyncio.to_thread(_dump_encrypt, local_path(name))
    async with db.session_factory()() as s:
        b = Backup(filename=name, size_bytes=size, sha256=sha, kind=kind, remotes=[], created_at=now)
        s.add(b)
        await s.commit()
    log.info("backup %s created (%d bytes)", name, size)
    copied = []
    for t in await targets():
        progress(f"uploading to {t.name}")
        try:
            await make_remote(t.type, secrets_crypto.decrypt_json(t.config_enc)).upload(local_path(name), name)
            copied.append(t.id)
        except Exception as exc:
            log.warning("upload of %s to %s failed: %s", name, t.name, exc)
            progress(f"upload to {t.name} FAILED: {exc}")
    if copied:
        async with db.session_factory()() as s:
            b = await s.get(Backup, b.id)
            b.remotes = copied
            await s.commit()
    await apply_retention()
    async with db.session_factory()() as s:
        return await s.get(Backup, b.id)


async def apply_retention() -> None:
    """Keep the newest N local backups (and 3 pre-restore ones); per-target N remotely."""
    keep_local = int(await _setting("backup.keep_local", 14))
    rows = await sync_local()
    for kind, keep in (("pre-restore", 3), (None, keep_local)):
        group = [b for b in rows if (b.kind == "pre-restore") == (kind == "pre-restore")]
        for b in group[keep:]:
            local_path(b.filename).unlink(missing_ok=True)
            log.info("retention: removed local %s", b.filename)
    present: dict[int, set[str]] = {}  # target id -> file names still there after pruning
    for t in await targets():
        try:
            r = make_remote(t.type, secrets_crypto.decrypt_json(t.config_enc))
            files = sorted(await r.list(), key=lambda f: f["name"], reverse=True)
            for f in files[t.keep:]:
                await r.delete(f["name"])
                log.info("retention: removed %s from %s", f["name"], t.name)
            present[t.id] = {f["name"] for f in files[:t.keep]}
        except Exception as exc:
            log.warning("remote retention on %s failed: %s", t.name, exc)
    if present:
        async with db.session_factory()() as s:
            for b in (await s.execute(select(Backup))).scalars():
                cur = list(b.remotes or [])
                new = [tid for tid in cur if tid not in present or b.filename in present[tid]]
                for tid, names in present.items():
                    if b.filename in names and tid not in new:
                        new.append(tid)
                if sorted(new) != sorted(cur):
                    b.remotes = new
            await s.commit()
    await sync_local()


async def _setting(key: str, default):
    async with db.session_factory()() as s:
        return await settings_store.get(s, key, default)


async def _set(key: str, value) -> None:
    async with db.session_factory()() as s:
        await settings_store.put(s, key, value)


async def restore(name: str, progress: Progress = lambda m: None) -> None:
    """Restore the database from a local backup. Takes a safety backup first and pauses the
    worker's ingest while pg_restore replaces the schema in a single transaction."""
    path = local_path(name)
    if not path.exists():
        raise FileNotFoundError(name)
    progress("verifying backup (decrypt + parse, database untouched)")
    await asyncio.to_thread(verify_file, path)
    progress("taking a safety backup of the current data")
    await create_backup("pre-restore", progress)
    progress("pausing ingest")
    await _set("maintenance", True)
    try:
        for _ in range(20):  # wait up to ~20s for the worker to acknowledge
            if await _setting("worker_paused", False):
                break
            await asyncio.sleep(1)
        progress("restoring database")
        await asyncio.to_thread(_restore_swap, path)
        progress("applying schema migrations")
        from ..cli import migrate
        await asyncio.to_thread(migrate)
    finally:
        try:
            await _set("maintenance", False)
        except Exception:
            log.exception("could not clear maintenance flag")
    await sync_local()
    progress("done")


async def datastore_stats() -> dict:
    async with db.engine().connect() as conn:
        size = (await conn.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
        parts = (await conn.execute(text("""
            SELECT c.relname AS part, coalesce(s.n_live_tup, 0) AS rows, pg_total_relation_size(c.oid) AS bytes
            FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_class p ON p.oid = i.inhparent AND p.relname = 'liquidations'
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid ORDER BY c.relname"""))).all()
        oldest = (await conn.execute(text("SELECT min(ts) FROM liquidations"))).scalar_one()
        total = (await conn.execute(text("SELECT count(*) FROM liquidations"))).scalar_one()
    return {"size": size, "oldest": oldest, "total": total,
            "partitions": [{"name": p.part, "rows": p.rows, "bytes": p.bytes} for p in parts]}


async def vacuum() -> None:
    async with db.engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM (ANALYZE)"))


async def prune(days: int) -> int:
    async with db.engine().begin() as conn:
        res = await conn.execute(text("DELETE FROM liquidations WHERE ts < now() - make_interval(days => :d)"),
                                 {"d": days})
        return res.rowcount
