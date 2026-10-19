from __future__ import annotations

import os
from pathlib import Path


def _secret(name: str, env: str, default: str = "") -> str:
    """Read a podman secret file if present, else fall back to an env var."""
    path = Path("/run/secrets") / name
    if path.is_file():
        return path.read_text().strip()
    return os.getenv(env, default).strip()


DB_HOST = os.getenv("DB_HOST", "liquidator-db")
DB_NAME = os.getenv("DB_NAME", "liquidator")
DB_USER = os.getenv("DB_USER", "liquidator")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/backups"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def db_password() -> str:
    return _secret("db_password", "DB_PASSWORD")


def app_key() -> str:
    """Fernet key used to encrypt feed/backup-target secrets stored in the DB."""
    return _secret("app_key", "APP_KEY")


def backup_key() -> str:
    """age X25519 identity (AGE-SECRET-KEY-...) used to encrypt and decrypt backups."""
    return _secret("backup_key", "BACKUP_KEY")


def async_dsn() -> str:
    return f"postgresql+asyncpg://{DB_USER}:{db_password()}@{DB_HOST}/{DB_NAME}"


def sync_dsn() -> str:
    return f"postgresql+psycopg2://{DB_USER}:{db_password()}@{DB_HOST}/{DB_NAME}"
