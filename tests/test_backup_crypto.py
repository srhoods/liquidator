import io

import pytest
from pyrage import x25519

from liquidator import config
from liquidator.backup import crypto


@pytest.fixture(autouse=True)
def key(monkeypatch):
    k = str(x25519.Identity.generate())
    monkeypatch.setattr(config, "backup_key", lambda: k)


def test_round_trip_and_hash():
    data = b"PGDMP" + bytes(range(256)) * 4000
    enc = io.BytesIO()
    w = crypto.HashingWriter(enc)
    crypto.encrypt_stream(io.BytesIO(data), w)
    assert w.size == len(enc.getvalue()) and crypto.looks_like_age(enc.getvalue())
    assert data not in enc.getvalue()
    out = io.BytesIO()
    crypto.decrypt_stream(io.BytesIO(enc.getvalue()), out)
    assert out.getvalue() == data


def test_wrong_key_fails(monkeypatch):
    enc = io.BytesIO()
    crypto.encrypt_stream(io.BytesIO(b"secret"), enc)
    monkeypatch.setattr(config, "backup_key", lambda: str(x25519.Identity.generate()))
    with pytest.raises(Exception):
        crypto.decrypt_stream(io.BytesIO(enc.getvalue()), io.BytesIO())
