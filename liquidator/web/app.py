from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .deps import BASE, LoginRequired
from .live import hub
from .routes import admin, api, data, feeds, pages

log = logging.getLogger(__name__)

CSP = ("default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'; form-action 'self'")


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(hub.listen_forever())
    yield
    task.cancel()


app = FastAPI(title="Liquidator", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.include_router(pages.router)
app.include_router(api.router)
app.include_router(admin.router)
app.include_router(feeds.router)
app.include_router(data.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if not request.url.path.startswith("/static"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.exception_handler(LoginRequired)
async def login_required(request: Request, _: LoginRequired):
    if request.url.path.startswith("/api") or request.headers.get("hx-request"):
        resp = JSONResponse({"detail": "login required"}, status_code=401)
        resp.headers["HX-Redirect"] = "/login"
        return resp
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.headers.get("hx-request") or request.url.path.startswith("/api"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    from .deps import render
    return render(request, "error.html", None, status_code=exc.status_code,
                  code=exc.status_code, detail=exc.detail)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
