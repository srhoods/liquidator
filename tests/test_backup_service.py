import os

import pytest
from pyrage import x25519

from liquidator import config
from liquidator.backup import crypto, service


@pytest.fixture
def encrypted(tmp_path, monkeypatch):
    k = str(x25519.Identity.generate())
    monkeypatch.setattr(config, "backup_key", lambda: k)
    plain = os.urandom(3 * 1024 * 1024)  # far larger than a pipe buffer (64 KiB)
    p = tmp_path / "b.age"
    with open(p, "wb") as f:
        crypto.encrypt_stream(__import__("io").BytesIO(plain), f)
    return p


def test_consumer_that_stops_reading_early_is_not_a_decrypt_error(encrypted):
    # pg_restore --list reads only the table of contents and exits; the rest must still decrypt cleanly
    service._run_decrypted(encrypted, ["head", "-c", "100"])


def test_consumer_failure_reports_its_own_error_not_a_pipe_error(encrypted):
    with pytest.raises(RuntimeError) as ei:
        service._run_decrypted(encrypted, ["sh", "-c", "echo 'not an archive' >&2; exit 3"])
    assert "rejected the backup" in str(ei.value) and "not an archive" in str(ei.value)
    assert "Broken pipe" not in str(ei.value)


def test_wrong_key_still_reported_as_decrypt_error(encrypted, monkeypatch):
    other = str(x25519.Identity.generate())
    monkeypatch.setattr(config, "backup_key", lambda: other)
    with pytest.raises(RuntimeError, match="could not decrypt"):
        service._run_decrypted(encrypted, ["cat"])


def test_truncated_file_is_a_decrypt_error(encrypted):
    data = encrypted.read_bytes()
    encrypted.write_bytes(data[: len(data) // 2])
    with pytest.raises(RuntimeError, match="could not decrypt"):
        service._run_decrypted(encrypted, ["cat"])
