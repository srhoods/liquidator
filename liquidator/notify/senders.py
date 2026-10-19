"""Message formatting and delivery for Discord, Telegram, Slack and generic webhooks.

Every sender takes the feed's decrypted config and a `Message`, and raises
`SendError` on failure (with `retry_after` set when the service rate-limits us).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

TYPES = ("discord", "telegram", "slack", "webhook")
COLOR_LONG, COLOR_SHORT, COLOR_INFO = 0xD63B3B, 0x12925A, 0x2A6FDB


class SendError(Exception):
    def __init__(self, msg: str, retry_after: Optional[float] = None, permanent: bool = False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.permanent = permanent


@dataclass
class Message:
    kind: str  # 'events' | 'cascade' | 'test'
    events: list[dict] = field(default_factory=list)  # Liq.as_dict() dicts
    summary: dict = field(default_factory=dict)  # cascade: window_min, total_usd, long_usd, short_usd, count, top


def usd(v: float) -> str:
    v = float(v)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.2f}{suffix}"
    return f"${v:,.0f}"


def _arrow(side: str) -> str:
    return "▼" if side == "long" else "▲"


def event_line(e: dict) -> str:
    return (f"{_arrow(e['side'])} {e['exchange']} {e['symbol']} — {e['side']} liquidated "
            f"{usd(e['notional_usd'])} @ {e['price']}")


def cascade_text(s: dict) -> str:
    top = ", ".join(f"{a['asset']} {usd(a['usd'])}" for a in s.get("top", [])[:5])
    return (f"Liquidation cascade: {usd(s['total_usd'])} in {s['window_min']} min "
            f"({s['count']} events) — longs {usd(s['long_usd'])} / shorts {usd(s['short_usd'])}"
            + (f". Top: {top}" if top else ""))


def plain_text(m: Message) -> str:
    if m.kind == "cascade":
        return cascade_text(m.summary)
    prefix = "Test message from Liquidator\n" if m.kind == "test" else ""
    return prefix + "\n".join(event_line(e) for e in m.events)


def _check(r: httpx.Response) -> None:
    if r.status_code == 429:
        try:
            wait = float(r.json().get("retry_after") or r.headers.get("retry-after") or 5)
        except Exception:
            wait = 5.0
        raise SendError("rate limited (429)", retry_after=min(wait, 30))
    if r.status_code >= 500:
        raise SendError(f"server error {r.status_code}")
    if r.status_code >= 400:
        raise SendError(f"HTTP {r.status_code}: {r.text[:200]}", permanent=True)


async def send_discord(c: httpx.AsyncClient, cfg: dict, m: Message) -> None:
    if m.kind == "cascade":
        embeds = [{"title": "Liquidation cascade", "description": cascade_text(m.summary),
                   "color": COLOR_INFO}]
    else:
        embeds = [{
            "title": f"{e['symbol']} · {e['side']} liquidated {_arrow(e['side'])}",
            "description": f"**{usd(e['notional_usd'])}** @ {e['price']}",
            "color": COLOR_LONG if e["side"] == "long" else COLOR_SHORT,
            "fields": [{"name": "Exchange", "value": e["exchange"], "inline": True},
                       {"name": "Qty", "value": f"{e['qty']} {e['base_asset']}", "inline": True}],
            "timestamp": e["ts"],
        } for e in m.events[:10]]
    body = {"username": cfg.get("username") or "Liquidator", "embeds": embeds}
    if m.kind == "test":
        body["content"] = "Test message from Liquidator"
    _check(await c.post(cfg["url"], json=body))


async def send_telegram(c: httpx.AsyncClient, cfg: dict, m: Message) -> None:
    import html
    text = html.escape(plain_text(m))
    url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
    r = await c.post(url, json={"chat_id": cfg["chat_id"], "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": True})
    if r.status_code == 429:
        try:
            wait = float(r.json()["parameters"]["retry_after"])
        except Exception:
            wait = 5.0
        raise SendError("rate limited (429)", retry_after=min(wait, 30))
    _check(r)


async def send_slack(c: httpx.AsyncClient, cfg: dict, m: Message) -> None:
    text = plain_text(m)
    color = "#2a6fdb"
    if m.kind == "events" and m.events:
        color = "#d63b3b" if m.events[0]["side"] == "long" else "#12925a"
    _check(await c.post(cfg["url"], json={"text": text, "attachments": [{"color": color, "text": text}]}))


async def send_webhook(c: httpx.AsyncClient, cfg: dict, m: Message) -> None:
    body = json.dumps({"type": m.kind, "text": plain_text(m), "events": m.events,
                       "summary": m.summary or None}, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "liquidator/0.1"}
    if cfg.get("secret"):
        headers["X-Liquidator-Signature"] = "sha256=" + hmac.new(
            cfg["secret"].encode(), body, hashlib.sha256).hexdigest()
    _check(await c.post(cfg["url"], content=body, headers=headers))


SENDERS = {"discord": send_discord, "telegram": send_telegram, "slack": send_slack, "webhook": send_webhook}

SAMPLE_EVENT = {
    "ts": "2026-01-01T00:00:00+00:00", "exchange": "binance", "symbol": "BTCUSDT",
    "base_asset": "BTC", "side": "long", "price": "96000", "qty": "1.25", "notional_usd": "120000",
}


async def send_once(ftype: str, cfg: dict, m: Message) -> None:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
        try:
            await SENDERS[ftype](c, cfg, m)
        except httpx.HTTPError as exc:
            raise SendError(f"{type(exc).__name__}: {exc}"[:300]) from exc
        except KeyError as exc:
            raise SendError(f"missing config field {exc}", permanent=True) from exc


def validate_config(ftype: str, cfg: dict) -> Optional[str]:
    """Return an error string, or None if the config looks usable."""
    need = {"discord": ["url"], "slack": ["url"], "webhook": ["url"], "telegram": ["token", "chat_id"]}
    for k in need[ftype]:
        if not str(cfg.get(k, "")).strip():
            return f"{k} is required"
    if "url" in need[ftype] and not str(cfg["url"]).startswith(("https://", "http://")):
        return "url must start with http:// or https://"
    if ftype == "discord" and "discord" not in cfg["url"]:
        return "that doesn't look like a Discord webhook URL"
    return None
