from __future__ import annotations

import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..models import Session, User

ph = PasswordHasher()
SESSION_TTL = timedelta(hours=12)
MAX_FAILS, FAIL_WINDOW = 5, 300.0
_fails: dict[str, list[float]] = defaultdict(list)
# verifying against a dummy hash keeps unknown-user timing close to known-user timing
_DUMMY = ph.hash("not-a-real-password")


def too_many_attempts(ip: str) -> bool:
    now = time.monotonic()
    _fails[ip] = [t for t in _fails[ip] if now - t < FAIL_WINDOW]
    return len(_fails[ip]) >= MAX_FAILS


def record_failure(ip: str) -> None:
    _fails[ip].append(time.monotonic())


def clear_failures(ip: str) -> None:
    _fails.pop(ip, None)


async def create_user(username: str, password: str, role: str = "viewer") -> None:
    if len(password) < 10:
        raise ValueError("password must be at least 10 characters")
    async with db.session_factory()() as s:
        s.add(User(username=username, password_hash=ph.hash(password), role=role))
        await s.commit()


async def authenticate(s: AsyncSession, username: str, password: str) -> Optional[User]:
    user = (await s.execute(select(User).where(User.username == username))).scalar_one_or_none()
    try:
        ph.verify(user.password_hash if user else _DUMMY, password)
    except (VerifyMismatchError, InvalidHashError):
        return None
    if not user or not user.active:
        return None
    if ph.check_needs_rehash(user.password_hash):
        user.password_hash = ph.hash(password)
    return user


async def new_session(s: AsyncSession, user: User) -> Session:
    now = datetime.now(timezone.utc)
    await s.execute(delete(Session).where(Session.expires_at < now))
    sess = Session(id=secrets.token_urlsafe(32), user_id=user.id,
                   csrf=secrets.token_urlsafe(24), expires_at=now + SESSION_TTL)
    s.add(sess)
    await s.commit()
    return sess


async def lookup(s: AsyncSession, sid: str | None) -> Optional[tuple[User, Session]]:
    if not sid:
        return None
    sess = await s.get(Session, sid)
    if not sess or sess.expires_at < datetime.now(timezone.utc):
        return None
    user = await s.get(User, sess.user_id)
    if not user or not user.active:
        return None
    return user, sess


async def end_session(s: AsyncSession, sid: str | None) -> None:
    if sid:
        await s.execute(delete(Session).where(Session.id == sid))
        await s.commit()
