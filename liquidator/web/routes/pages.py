from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import CollectorStatus
from .. import auth, stats
from ..deps import COOKIE, Ctx, csrf_protect, current, get_db, render

router = APIRouter()
EXCHANGES = ["binance", "bybit", "okx"]


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html", None, error=None)


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...),
                s: AsyncSession = Depends(get_db)):
    ip = _ip(request)
    if auth.too_many_attempts(ip):
        return render(request, "login.html", None, status_code=429,
                      error="Too many failed attempts. Try again in a few minutes.")
    user = await auth.authenticate(s, username.strip(), password)
    if not user:
        auth.record_failure(ip)
        return render(request, "login.html", None, status_code=401,
                      error="Invalid username or password.")
    auth.clear_failures(ip)
    sess = await auth.new_session(s, user)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, sess.id, httponly=True, secure=True, samesite="lax",
                    max_age=int(auth.SESSION_TTL.total_seconds()), path="/")
    return resp


@router.post("/logout")
async def logout(request: Request, ctx: Ctx = Depends(csrf_protect), s: AsyncSession = Depends(get_db)):
    await auth.end_session(s, ctx.sid)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/")
async def dashboard(request: Request, ctx: Ctx = Depends(current)):
    return render(request, "dashboard.html", ctx, page="dashboard", exchanges=EXCHANGES)


def _parse_dt(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _filters(q) -> dict:
    hours = q.get("hours")
    since = _parse_dt(q.get("since"))
    if hours and hours.isdigit():
        since = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    try:
        min_usd = float(q.get("min_usd") or 0) or None
    except ValueError:
        min_usd = None
    ex = q.get("exchange") if q.get("exchange") in EXCHANGES else None
    side = q.get("side") if q.get("side") in ("long", "short") else None
    return {"since": since, "until": _parse_dt(q.get("until")), "exchange": ex,
            "asset": (q.get("asset") or "").strip() or None, "side": side, "min_usd": min_usd}


@router.get("/history")
async def history(request: Request, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    q = request.query_params
    f = _filters(q)
    before = None
    if q.get("b_ts") and q.get("b_id", "").isdigit():
        d = _parse_dt(q["b_ts"])
        before = (d, int(q["b_id"])) if d else None
    limit = 100
    rows = await stats.history(s, **f, before=before, limit=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    next_qs = None
    if more:
        keep = {k: v for k, v in q.items() if k not in ("b_ts", "b_id") and v}
        keep.update(b_ts=rows[-1]["ts"], b_id=str(rows[-1]["id"]))
        from urllib.parse import urlencode
        next_qs = urlencode(keep)
    tpl = "history_rows.html" if request.headers.get("hx-request") else "history.html"
    return render(request, tpl, ctx, page="history", rows=rows, next_qs=next_qs, q=q,
                  exchanges=EXCHANGES)


@router.get("/history.csv")
async def history_csv(request: Request, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    f = _filters(request.query_params)
    rows = await stats.history(s, **f, limit=100_000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "exchange", "symbol", "asset", "liquidated_side", "price", "qty", "notional_usd"])
    for r in rows:
        w.writerow([r["ts"], r["exchange"], r["symbol"], r["base_asset"], r["side"], r["price"],
                    r["qty"], f"{r['notional_usd']:.2f}"])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="liquidations.csv"'})


@router.get("/status")
async def status(request: Request, ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    rows = (await s.execute(select(CollectorStatus).order_by(CollectorStatus.name))).scalars().all()
    now = datetime.now(timezone.utc)
    worker_alive = bool(rows) and max(r.updated_at for r in rows) > now - timedelta(seconds=45)
    return render(request, "status.html", ctx, page="status", collectors=rows,
                  worker_alive=worker_alive, now=now)
