from __future__ import annotations

import html

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ... import crypto
from ...models import Feed, FeedDelivery
from ...notify import senders
from ..deps import Ctx, admin, admin_post, audit, get_db, render

router = APIRouter(prefix="/admin/feeds")
EXCHANGES = ["binance", "bybit", "okx"]
SECRET_FIELDS = {"url", "token", "secret"}  # never echoed back to the browser
CONFIG_FIELDS = {"discord": ["url", "username"], "telegram": ["token", "chat_id"],
                 "slack": ["url"], "webhook": ["url", "secret"]}


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


async def _parse(request: Request, ftype: str, existing_cfg: dict | None = None):
    form = await request.form()
    cfg = dict(existing_cfg or {})
    for k in CONFIG_FIELDS[ftype]:
        v = (form.get(k) or "").strip()
        if v or k not in SECRET_FIELDS:
            cfg[k] = v  # blank secret field on edit keeps the stored value
    filters = {
        "exchanges": [e for e in form.getlist("exchanges") if e in EXCHANGES],
        "sides": [s for s in form.getlist("sides") if s in ("long", "short")],
        "assets": [a.strip().upper() for a in (form.get("assets") or "").replace(";", ",").split(",") if a.strip()],
        "min_usd": max(0.0, _num(form.get("min_usd"), 0)),
    }
    mode = form.get("mode") if form.get("mode") in ("event", "cascade") else "event"
    mode_cfg = {"window_min": max(1.0, _num(form.get("window_min"), 5)),
                "threshold_usd": max(1.0, _num(form.get("threshold_usd"), 1_000_000)),
                "cooldown_min": max(1.0, _num(form.get("cooldown_min"), 15))}
    return {
        "name": (form.get("name") or "").strip()[:100], "enabled": form.get("enabled") == "on",
        "cfg": cfg, "filters": filters, "mode": mode, "mode_cfg": mode_cfg,
        "rate": int(max(1, min(600, _num(form.get("rate_limit_per_min"), 30)))),
    }


def _view(f: Feed | None, ftype: str = "discord") -> dict:
    """Template data for the form; secrets are replaced by a 'set' flag."""
    if not f:
        return {"id": None, "name": "", "type": ftype, "enabled": True, "cfg": {}, "secrets_set": {},
                "filters": {}, "mode": "event", "mode_cfg": {}, "rate": 30}
    cfg = crypto.decrypt_json(f.config_enc)
    return {"id": f.id, "name": f.name, "type": f.type, "enabled": f.enabled,
            "cfg": {k: v for k, v in cfg.items() if k not in SECRET_FIELDS},
            "secrets_set": {k: bool(cfg.get(k)) for k in SECRET_FIELDS},
            "filters": f.filters or {}, "mode": f.mode, "mode_cfg": f.mode_cfg or {},
            "rate": f.rate_limit_per_min}


@router.get("")
async def list_feeds(request: Request, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    feeds = (await s.execute(select(Feed).order_by(Feed.id))).scalars().all()
    last = {}
    for f in feeds:
        d = (await s.execute(select(FeedDelivery).where(FeedDelivery.feed_id == f.id)
                             .order_by(desc(FeedDelivery.id)).limit(1))).scalar_one_or_none()
        last[f.id] = d
    return render(request, "feeds.html", ctx, page="feeds", feeds=feeds, last=last)


@router.get("/new")
async def new_feed(request: Request, type: str = "discord", ctx: Ctx = Depends(admin)):
    if type not in senders.TYPES:
        type = "discord"
    return render(request, "feed_form.html", ctx, page="feeds", f=_view(None, type), error=None,
                  exchanges=EXCHANGES, deliveries=[])


@router.post("")
async def create_feed(request: Request, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    form = await request.form()
    ftype = form.get("type")
    if ftype not in senders.TYPES:
        raise HTTPException(400, "bad type")
    d = await _parse(request, ftype)
    err = None if d["name"] else "Name is required."
    err = err or senders.validate_config(ftype, d["cfg"])
    if err:
        view = {"id": None, "type": ftype, "name": d["name"], "enabled": d["enabled"],
                "cfg": {k: v for k, v in d["cfg"].items() if k not in SECRET_FIELDS},
                "secrets_set": {}, "filters": d["filters"], "mode": d["mode"],
                "mode_cfg": d["mode_cfg"], "rate": d["rate"]}
        return render(request, "feed_form.html", ctx, status_code=400, page="feeds", f=view,
                      error=err, exchanges=EXCHANGES, deliveries=[])
    f = Feed(name=d["name"], type=ftype, enabled=d["enabled"], config_enc=crypto.encrypt_json(d["cfg"]),
             filters=d["filters"], mode=d["mode"], mode_cfg=d["mode_cfg"], rate_limit_per_min=d["rate"])
    s.add(f)
    await s.commit()
    await audit(s, ctx, "feed.create", f"{f.name} ({ftype})")
    return RedirectResponse(f"/admin/feeds/{f.id}", status_code=303)


@router.get("/{fid}")
async def edit_feed(fid: int, request: Request, ctx: Ctx = Depends(admin), s: AsyncSession = Depends(get_db)):
    f = await s.get(Feed, fid)
    if not f:
        raise HTTPException(404, "no such feed")
    deliveries = (await s.execute(select(FeedDelivery).where(FeedDelivery.feed_id == fid)
                                  .order_by(desc(FeedDelivery.id)).limit(30))).scalars().all()
    return render(request, "feed_form.html", ctx, page="feeds", f=_view(f), error=None,
                  exchanges=EXCHANGES, deliveries=deliveries)


@router.post("/{fid}")
async def update_feed(fid: int, request: Request, ctx: Ctx = Depends(admin_post),
                      s: AsyncSession = Depends(get_db)):
    f = await s.get(Feed, fid)
    if not f:
        raise HTTPException(404, "no such feed")
    d = await _parse(request, f.type, crypto.decrypt_json(f.config_enc))
    err = None if d["name"] else "Name is required."
    err = err or senders.validate_config(f.type, d["cfg"])
    if err:
        view = _view(f)
        view.update(name=d["name"], filters=d["filters"], mode=d["mode"], mode_cfg=d["mode_cfg"],
                    rate=d["rate"], enabled=d["enabled"])
        return render(request, "feed_form.html", ctx, status_code=400, page="feeds", f=view,
                      error=err, exchanges=EXCHANGES, deliveries=[])
    f.name, f.enabled = d["name"], d["enabled"]
    f.config_enc = crypto.encrypt_json(d["cfg"])
    f.filters, f.mode, f.mode_cfg, f.rate_limit_per_min = d["filters"], d["mode"], d["mode_cfg"], d["rate"]
    await s.commit()
    await audit(s, ctx, "feed.update", f.name)
    return RedirectResponse(f"/admin/feeds/{fid}?saved=1", status_code=303)


@router.post("/{fid}/toggle")
async def toggle_feed(fid: int, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    f = await s.get(Feed, fid)
    if not f:
        raise HTTPException(404, "no such feed")
    f.enabled = not f.enabled
    await s.commit()
    await audit(s, ctx, "feed.toggle", f"{f.name} enabled={f.enabled}")
    return RedirectResponse("/admin/feeds", status_code=303)


@router.post("/{fid}/delete")
async def delete_feed(fid: int, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    f = await s.get(Feed, fid)
    if not f:
        raise HTTPException(404, "no such feed")
    await s.execute(delete(FeedDelivery).where(FeedDelivery.feed_id == fid))
    await s.delete(f)
    await s.commit()
    await audit(s, ctx, "feed.delete", f.name)
    return RedirectResponse("/admin/feeds", status_code=303)


@router.post("/{fid}/test")
async def test_feed(fid: int, ctx: Ctx = Depends(admin_post), s: AsyncSession = Depends(get_db)):
    """Send one sample message using the saved config; returns an HTML snippet for htmx."""
    f = await s.get(Feed, fid)
    if not f:
        raise HTTPException(404, "no such feed")
    msg = senders.Message("test", events=[senders.SAMPLE_EVENT])
    try:
        await senders.send_once(f.type, crypto.decrypt_json(f.config_enc), msg)
        result, cls = "Test message sent.", "ok"
    except senders.SendError as exc:
        result, cls = f"Failed: {exc}", "err"
    s.add(FeedDelivery(feed_id=fid, ok=cls == "ok", detail=f"test: {result}"))
    await s.commit()
    await audit(s, ctx, "feed.test", f"{f.name}: {result}")
    return HTMLResponse(f'<span class="{cls}">{html.escape(result)}</span>')
