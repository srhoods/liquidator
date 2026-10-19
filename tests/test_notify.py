import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest

from liquidator.collectors.base import Liq
from liquidator.notify import senders
from liquidator.notify.dispatcher import Dispatcher, FeedState, matches
from liquidator.notify.senders import Message, SendError


def liq(usd="50000", ex="binance", asset="BTC", side="long", ts=None):
    return Liq(ts or datetime.now(timezone.utc), ex, asset + "USDT", asset, side,
               D("96000"), D("1"), D(usd))


def test_matches_filters():
    f = {"exchanges": ["binance"], "assets": ["btc"], "sides": ["long"], "min_usd": 10000}
    assert matches(f, liq())
    assert not matches(f, liq(ex="okx"))
    assert not matches(f, liq(asset="ETH"))
    assert not matches(f, liq(side="short"))
    assert not matches(f, liq(usd="9999"))
    assert matches({}, liq(usd="1"))


def test_usd_formatting():
    assert senders.usd(1_234_567) == "$1.23M"
    assert senders.usd(999) == "$999"
    assert senders.usd(2_500_000_000) == "$2.50B"


def test_event_line_arrows():
    assert senders.event_line(liq().as_dict()).startswith("▼")
    assert senders.event_line(liq(side="short").as_dict()).startswith("▲")


async def _run(sender, cfg, msg, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await sender(c, cfg, msg)


async def test_discord_embed_colours_and_payload():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(204)

    msg = Message("events", events=[liq().as_dict(), liq(side="short").as_dict()])
    await _run(senders.send_discord, {"url": "https://discord.com/api/webhooks/1/x"}, msg, handler)
    e = seen["body"]["embeds"]
    assert e[0]["color"] == senders.COLOR_LONG and e[1]["color"] == senders.COLOR_SHORT
    assert "$50.00K" in e[0]["description"]


async def test_webhook_signature():
    seen = {}

    def handler(req):
        seen["req"] = req
        return httpx.Response(200)

    await _run(senders.send_webhook, {"url": "https://x.test/h", "secret": "s3cret"},
               Message("events", events=[liq().as_dict()]), handler)
    req = seen["req"]
    want = "sha256=" + hmac.new(b"s3cret", req.content, hashlib.sha256).hexdigest()
    assert req.headers["x-liquidator-signature"] == want


async def test_rate_limit_and_errors():
    def limited(req):
        return httpx.Response(429, json={"retry_after": 2.5})

    with pytest.raises(SendError) as ei:
        await _run(senders.send_discord, {"url": "https://discord.com/x"}, Message("test", events=[senders.SAMPLE_EVENT]), limited)
    assert ei.value.retry_after == 2.5 and not ei.value.permanent

    with pytest.raises(SendError) as ei:
        await _run(senders.send_slack, {"url": "https://hooks.slack.com/x"}, Message("test", events=[senders.SAMPLE_EVENT]),
                   lambda r: httpx.Response(404, text="no_service"))
    assert ei.value.permanent


def test_validate_config():
    assert senders.validate_config("discord", {"url": "https://discord.com/api/webhooks/1/a"}) is None
    assert senders.validate_config("discord", {"url": "https://evil.example/x"})
    assert senders.validate_config("telegram", {"token": "1:a"})  # chat_id missing
    assert senders.validate_config("webhook", {"url": "ftp://x"})


def _state(threshold=100000, window=5, cooldown=15):
    feed = SimpleNamespace(name="t", type="webhook", mode="cascade", id=1,
                           mode_cfg={"window_min": window, "threshold_usd": threshold, "cooldown_min": cooldown})
    return FeedState(feed=feed, cfg={})


async def test_cascade_fires_once_then_cooldown(monkeypatch):
    d, st, sent = Dispatcher(), _state(), []

    async def fake(self_, st_, msg, n, note=""):
        sent.append(msg)
    monkeypatch.setattr(Dispatcher, "_deliver", fake)
    d._cascade(st, [liq("60000")])
    d._cascade(st, [liq("60000", asset="ETH", side="short")])
    import asyncio
    await asyncio.sleep(0)
    assert len(sent) == 1
    s = sent[0].summary
    assert s["total_usd"] == 120000 and s["long_usd"] == 60000 and s["short_usd"] == 60000
    d._cascade(st, [liq("500000")])  # still in cooldown
    await asyncio.sleep(0)
    assert len(sent) == 1


async def test_cascade_ignores_old_events(monkeypatch):
    d, st, sent = Dispatcher(), _state(), []

    async def fake(self_, st_, msg, n, note=""):
        sent.append(msg)
    monkeypatch.setattr(Dispatcher, "_deliver", fake)
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    d._cascade(st, [liq("90000", ts=old)])
    d._cascade(st, [liq("90000")])
    import asyncio
    await asyncio.sleep(0)
    assert sent == []
