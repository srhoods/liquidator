"""Remote backup targets: S3-compatible object storage and SFTP."""
from __future__ import annotations

import asyncio
import posixpath
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

NAME_SUFFIX = ".dump.age"


class Remote(Protocol):
    async def test(self) -> str: ...
    async def upload(self, path: Path, name: str) -> None: ...
    async def list(self) -> list[dict]: ...
    async def download(self, name: str, dest: Path) -> None: ...
    async def delete(self, name: str) -> None: ...


def _ours(name: str) -> bool:
    return name.startswith("liquidator-") and name.endswith(NAME_SUFFIX)


class S3Remote:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.prefix = (cfg.get("prefix") or "").strip("/")
        self.bucket = cfg["bucket"]

    def _client(self):
        import boto3
        from botocore.config import Config
        return boto3.client(
            "s3", endpoint_url=self.cfg.get("endpoint_url") or None,
            region_name=self.cfg.get("region") or None,
            aws_access_key_id=self.cfg["access_key"], aws_secret_access_key=self.cfg["secret_key"],
            config=Config(retries={"max_attempts": 3}, connect_timeout=10, read_timeout=60,
                          s3={"addressing_style": "path" if self.cfg.get("endpoint_url") else "auto"}))

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    async def test(self) -> str:
        def run() -> str:
            c = self._client()
            c.head_bucket(Bucket=self.bucket)
            return f"bucket {self.bucket!r} reachable"
        return await asyncio.to_thread(run)

    async def upload(self, path: Path, name: str) -> None:
        await asyncio.to_thread(self._client().upload_file, str(path), self.bucket, self._key(name))

    async def list(self) -> list[dict]:
        def run() -> list[dict]:
            out, token = [], None
            c = self._client()
            while True:
                kw = {"Bucket": self.bucket, "Prefix": (self.prefix + "/") if self.prefix else ""}
                if token:
                    kw["ContinuationToken"] = token
                r = c.list_objects_v2(**kw)
                for o in r.get("Contents", []):
                    n = posixpath.basename(o["Key"])
                    if _ours(n):
                        out.append({"name": n, "size": o["Size"], "mtime": o["LastModified"]})
                if not r.get("IsTruncated"):
                    return out
                token = r["NextContinuationToken"]
        return await asyncio.to_thread(run)

    async def download(self, name: str, dest: Path) -> None:
        await asyncio.to_thread(self._client().download_file, self.bucket, self._key(name), str(dest))

    async def delete(self, name: str) -> None:
        await asyncio.to_thread(self._client().delete_object, Bucket=self.bucket, Key=self._key(name))


class SftpRemote:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.path = cfg.get("path") or "."

    async def _connect(self):
        import asyncssh
        kw = {"host": self.cfg["host"], "port": int(self.cfg.get("port") or 22),
              "username": self.cfg["username"], "connect_timeout": 15}
        if self.cfg.get("private_key"):
            kw["client_keys"] = [asyncssh.import_private_key(self.cfg["private_key"],
                                                              self.cfg.get("key_passphrase") or None)]
        if self.cfg.get("password"):
            kw["password"] = self.cfg["password"]
        if not self.cfg.get("host_key"):
            raise RuntimeError("no pinned host key; re-save the target to fetch it")
        kw["known_hosts"] = asyncssh.import_known_hosts(f"{self.cfg['host']} {self.cfg['host_key']}")
        return await asyncssh.connect(**kw)

    async def test(self) -> str:
        async with await self._connect() as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.listdir(self.path)
        return f"{self.cfg['host']}:{self.path} reachable"

    async def upload(self, path: Path, name: str) -> None:
        async with await self._connect() as conn:
            async with conn.start_sftp_client() as sftp:
                tmp = posixpath.join(self.path, name + ".part")
                await sftp.put(str(path), tmp)
                await sftp.chmod(tmp, 0o600)
                await sftp.rename(tmp, posixpath.join(self.path, name))

    async def list(self) -> list[dict]:
        async with await self._connect() as conn:
            async with conn.start_sftp_client() as sftp:
                out = []
                for e in await sftp.readdir(self.path):
                    if _ours(e.filename):
                        out.append({"name": e.filename, "size": e.attrs.size or 0,
                                    "mtime": datetime.fromtimestamp(e.attrs.mtime or 0, tz=timezone.utc)})
                return out

    async def download(self, name: str, dest: Path) -> None:
        async with await self._connect() as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.get(posixpath.join(self.path, name), str(dest))

    async def delete(self, name: str) -> None:
        async with await self._connect() as conn:
            async with conn.start_sftp_client() as sftp:
                await sftp.remove(posixpath.join(self.path, name))


def make_remote(ttype: str, cfg: dict) -> Remote:
    return {"s3": S3Remote, "sftp": SftpRemote}[ttype](cfg)


async def fetch_sftp_host_key(host: str, port: int) -> tuple[str, str]:
    """Return (openssh public key line, SHA256 fingerprint) of the server, for pinning."""
    import asyncssh
    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port), 15)
    if key is None:
        raise RuntimeError("server did not present a host key")
    return (key.export_public_key("openssh").decode().strip(),
            f"{key.algorithm.decode()} {key.get_fingerprint('sha256')}")


def validate(ttype: str, cfg: dict) -> str | None:
    need = {"s3": ["bucket", "access_key", "secret_key"], "sftp": ["host", "username", "path"]}[ttype]
    for k in need:
        if not str(cfg.get(k, "")).strip():
            return f"{k} is required"
    if ttype == "sftp" and not (cfg.get("password") or cfg.get("private_key")):
        return "provide a password or a private key"
    return None
