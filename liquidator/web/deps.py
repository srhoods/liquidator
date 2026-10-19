from __future__ import annotations

import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..models import AuditLog, User
from . import auth

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
COOKIE = "sid"


class LoginRequired(Exception):
    pass


@dataclass
class Ctx:
    user: User
    csrf: str
    sid: str

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


async def get_db() -> AsyncIterator[AsyncSession]:
    async with db.session_factory()() as s:
        yield s


async def current(request: Request, s: AsyncSession = Depends(get_db)) -> Ctx:
    sid = request.cookies.get(COOKIE)
    found = await auth.lookup(s, sid)
    if not found:
        raise LoginRequired()
    user, sess = found
    return Ctx(user=user, csrf=sess.csrf, sid=sess.id)


async def admin(ctx: Ctx = Depends(current)) -> Ctx:
    if not ctx.is_admin:
        raise HTTPException(403, "admin only")
    return ctx


async def csrf_protect(request: Request, ctx: Ctx = Depends(current)) -> Ctx:
    """State-changing requests must echo the session's CSRF token (header or form field)."""
    token = request.headers.get("x-csrf-token")
    if not token and "form" in request.headers.get("content-type", ""):
        token = (await request.form()).get("csrf")
    if not token or not hmac.compare_digest(token, ctx.csrf):
        raise HTTPException(403, "bad CSRF token")
    return ctx


async def admin_post(request: Request, ctx: Ctx = Depends(csrf_protect)) -> Ctx:
    if not ctx.is_admin:
        raise HTTPException(403, "admin only")
    return ctx


async def audit(s: AsyncSession, ctx: Ctx, action: str, detail: str = "") -> None:
    s.add(AuditLog(actor=ctx.user.username, action=action, detail=detail[:2000]))
    await s.commit()


def render(request: Request, name: str, ctx: Ctx | None, status_code: int = 200, **kw):
    return templates.TemplateResponse(
        request, name, {"ctx": ctx, "csrf": ctx.csrf if ctx else "", **kw}, status_code=status_code)
