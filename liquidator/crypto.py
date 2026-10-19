"""Fernet encryption for secrets stored in the database (webhook URLs, tokens, credentials)."""
from __future__ import annotations

import json
from functools import lru_cache

from cryptography.fernet import Fernet

from . import config


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(config.app_key().encode())


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(token: str) -> dict:
    return json.loads(_fernet().decrypt(token.encode()))
