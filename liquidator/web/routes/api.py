from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from ... import db
from .. import auth, stats
from ..deps import COOKIE, Ctx, current, get_db
from ..live import hub

router = APIRouter(prefix="/api")


@router.get("/stats")
async def get_stats(range: str = Query("24h"), ctx: Ctx = Depends(current),
                    s: AsyncSession = Depends(get_db)):
    if range not in stats.RANGES:
        raise HTTPException(400, "bad range")
    return await stats.dashboard(s, range)


@router.get("/recent")
async def get_recent(ctx: Ctx = Depends(current), s: AsyncSession = Depends(get_db)):
    return await stats.recent(s)


@router.websocket("/live")
async def live(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != ws.headers.get("host"):
        await ws.close(code=4403)
        return
    async with db.session_factory()() as s:
        found = await auth.lookup(s, ws.cookies.get(COOKIE))
    if not found:
        await ws.close(code=4401)
        return
    await ws.accept()
    hub.clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # client pings keep the connection observed
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(ws)
