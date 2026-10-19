"""Streaming age encryption for backups. Files are standard age (X25519) and can be
decrypted outside the app with:  age -d -i backup.key file.dump.age > file.dump"""
from __future__ import annotations

import hashlib
from typing import BinaryIO

import pyrage
from pyrage import x25519

from .. import config

MAGIC = b"age-encryption.org/v1"


def identity() -> x25519.Identity:
    key = config.backup_key()
    if not key:
        raise RuntimeError("backup key is not configured (podman secret liquidator_backup_key)")
    return x25519.Identity.from_str(key)


def public_key() -> str:
    return str(identity().to_public())


class HashingWriter:
    """File-like writer that tracks size and SHA-256 of what passes through."""

    def __init__(self, f: BinaryIO) -> None:
        self.f, self.sha, self.size = f, hashlib.sha256(), 0

    def write(self, b: bytes) -> int:
        self.sha.update(b)
        self.size += len(b)
        return self.f.write(b)

    def flush(self) -> None:
        self.f.flush()


class Sink:
    def write(self, b: bytes) -> int:
        return len(b)

    def flush(self) -> None:
        pass


def encrypt_stream(reader: BinaryIO, writer) -> None:
    pyrage.encrypt_io(reader, writer, [identity().to_public()])


def decrypt_stream(reader: BinaryIO, writer) -> None:
    pyrage.decrypt_io(reader, writer, [identity()])


def looks_like_age(head: bytes) -> bool:
    return head.startswith(MAGIC)
