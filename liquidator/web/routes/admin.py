from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import AuditLog, Session, User
from .. import auth
from ..deps import Ctx, admin, admin_post, audit, get_db, render

router = APIRouter(prefix="/admin")


async def _users_page(request, ctx, s, error=None, status_code=200):
    users = (await s.execute(select(User).order_by(User.username))).scalars().all()
    return render(request, "users.html", ctx, status_code=status_code, page="users",
                  users=users, error=error)


@router.get("/users")
async def users(request: Request, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    return await _users_page(request, ctx, s)


@router.post("/users")
async def add_user(request: Request, username: str = Form(...), password: str = Form(...),
                   role: str = Form("viewer"), ctx: Ctx = Depends(admin_post),
                   s: AsyncSession = Depends(get_db)):
    username = username.strip()
    if role not in ("admin", "viewer") or not username:
        raise HTTPException(400, "bad input")
    if len(password) < 10:
        return await _users_page(request, ctx, s, "Password must be at least 10 characters.", 400)
    if (await s.execute(select(User).where(User.username == username))).scalar_one_or_none():
        return await _users_page(request, ctx, s, "That username already exists.", 400)
    s.add(User(username=username, password_hash=auth.ph.hash(password), role=role))
    await s.commit()
    await audit(s, ctx, "user.create", f"{username} ({role})")
    return RedirectResponse("/admin/users", status_code=303)


async def _admin_count(s: AsyncSession) -> int:
    return (await s.execute(select(func.count()).select_from(User)
                            .where(User.role == "admin", User.active))).scalar_one()


@router.post("/users/{uid}/update")
async def update_user(uid: int, request: Request, role: str = Form(...), active: str = Form("off"),
                      password: str = Form(""), ctx: Ctx = Depends(admin_post),
                      s: AsyncSession = Depends(get_db)):
    u = await s.get(User, uid)
    if not u or role not in ("admin", "viewer"):
        raise HTTPException(404, "no such user")
    want_active = active == "on"
    losing_admin = u.role == "admin" and u.active and (role != "admin" or not want_active)
    if losing_admin and await _admin_count(s) <= 1:
        return await _users_page(request, ctx, s, "You can't remove the last active admin.", 400)
    if password:
        if len(password) < 10:
            return await _users_page(request, ctx, s, "Password must be at least 10 characters.", 400)
        u.password_hash = auth.ph.hash(password)
    u.role, u.active = role, want_active
    if password or not want_active:
        await s.execute(delete(Session).where(Session.user_id == u.id))
    await s.commit()
    await audit(s, ctx, "user.update", f"{u.username} role={role} active={want_active} pw={'yes' if password else 'no'}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{uid}/delete")
async def delete_user(uid: int, request: Request, ctx: Ctx = Depends(admin_post),
                      s: AsyncSession = Depends(get_db)):
    u = await s.get(User, uid)
    if not u:
        raise HTTPException(404, "no such user")
    if u.id == ctx.user.id:
        return await _users_page(request, ctx, s, "You can't delete your own account.", 400)
    if u.role == "admin" and u.active and await _admin_count(s) <= 1:
        return await _users_page(request, ctx, s, "You can't delete the last active admin.", 400)
    await s.execute(delete(Session).where(Session.user_id == u.id))
    await s.delete(u)
    await s.commit()
    await audit(s, ctx, "user.delete", u.username)
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/audit")
async def audit_log(request: Request, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    rows = (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(300))).scalars().all()
    return render(request, "audit.html", ctx, page="audit", rows=rows)
