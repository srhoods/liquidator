from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
import urllib.request

from . import config


def _logging() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    )


def migrate() -> None:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")


def main() -> None:
    parser = argparse.ArgumentParser(prog="liquidator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("web")
    sub.add_parser("worker")
    sub.add_parser("migrate")
    sub.add_parser("healthcheck")
    ca = sub.add_parser("create-admin")
    ca.add_argument("username")
    args = parser.parse_args()
    _logging()

    if args.cmd == "migrate":
        migrate()
    elif args.cmd == "web":
        import uvicorn

        migrate()
        uvicorn.run("liquidator.web.app:app", host="0.0.0.0", port=8000,
                    proxy_headers=True, forwarded_allow_ips="*", log_level="info")
    elif args.cmd == "worker":
        from .ingest.worker import run

        asyncio.run(run())
    elif args.cmd == "healthcheck":
        try:
            port = 8000
            if urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3).status == 200:
                return
        except Exception:
            pass
        # worker container has no HTTP server: fall back to heartbeat file
        from pathlib import Path
        import time

        hb = Path("/tmp/worker.heartbeat")
        if hb.exists() and time.time() - hb.stat().st_mtime < 120:
            return
        sys.exit(1)
    elif args.cmd == "create-admin":
        from .web.auth import create_user

        pw = getpass.getpass("Password: ")
        asyncio.run(create_user(args.username, pw, "admin"))
        print(f"admin user {args.username!r} created")
