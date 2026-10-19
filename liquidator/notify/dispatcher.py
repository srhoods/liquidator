"""Matches stored events to feeds and delivers them (batched, rate-limited, retried)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text

from .. import crypto, db
from ..collectors.base import Liq
from ..models import Feed, FeedDelivery
from .senders import Message, SendError, send_once

log = logging.getLogger(__name__)
BATCH_WINDOW = 1.5  # seconds to gather events into one message
BATCH_MAX = 10
RETRIES = 3
KEEP_DELIVERIES = 500


def matches(f: dict, l: Liq) -> bool:
    ex = f.get("exchanges") or []
    assets = [a.upper() for a in (f.get("assets") or [])]
    sides = f.get("sides") or []
    return ((not ex or l.exchange in ex)
            and (not assets or l.base_asset.upper() in assets)
            and (not sides or l.side in sides)
            and float(l.notional_usd) >= float(f.get("min_usd") or 0))


@dataclass
class FeedState:
    feed: Feed
    cfg: dict
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2000))
    sent: deque = field(default_factory=deque)  # monotonic send times (rate limit)
    window: deque = field(default_factory=deque)  # cascade: (datetime, usd, side, asset)
    cooldown_until: float = 0.0
    task: asyncio.Task | None = None
    dropped: int = 0


class Dispatcher:
    def __init__(self) -> None:
        self.states: dict[int, FeedState] = {}

    async def reload_forever(self) -> None:
        while True:
            try:
                await self.reload()
            except Exception:
                log.exception("feed reload failed")
            await asyncio.sleep(10)

    async def reload(self) -> None:
        async with db.session_factory()() as s:
            feeds = (await s.execute(select(Feed).where(Feed.enabled))).scalars().all()
        seen = set()
        for f in feeds:
            seen.add(f.id)
            st = self.states.get(f.id)
            sig = (f.config_enc, str(f.filters), f.mode, str(f.mode_cfg), f.rate_limit_per_min)
            if st and getattr(st, "sig", None) == sig:
                continue
            try:
                cfg = crypto.decrypt_json(f.config_enc)
            except Exception:
                log.error("feed %s: cannot decrypt config (app key changed?)", f.name)
                continue
            if st and st.task:
                st.task.cancel()
            st = FeedState(feed=f, cfg=cfg)
            st.sig = sig  # type: ignore[attr-defined]
            st.task = asyncio.create_task(self._worker(st), name=f"feed-{f.id}")
            self.states[f.id] = st
            log.info("feed %r active (%s, %s)", f.name, f.type, f.mode)
        for fid in list(self.states):
            if fid not in seen:
                t = self.states.pop(fid).task
                if t:
                    t.cancel()

    async def on_events(self, events: list[Liq]) -> None:
        for st in list(self.states.values()):
            f = st.feed
            mine = [l for l in events if matches(f.filters or {}, l)]
            if not mine:
                continue
            if f.mode == "cascade":
                self._cascade(st, mine)
            else:
                for l in mine:
                    try:
                        st.queue.put_nowait(l)
                    except asyncio.QueueFull:
                        st.dropped += 1

    def _cascade(self, st: FeedState, events: list[Liq]) -> None:
        cfg = st.feed.mode_cfg or {}
        window = timedelta(minutes=float(cfg.get("window_min") or 5))
        threshold = float(cfg.get("threshold_usd") or 1_000_000)
        cooldown = float(cfg.get("cooldown_min") or 15) * 60
        now = datetime.now(timezone.utc)
        for l in events:
            st.window.append((l.ts, float(l.notional_usd), l.side, l.base_asset))
        while st.window and st.window[0][0] < now - window:
            st.window.popleft()
        total = sum(w[1] for w in st.window)
        if total >= threshold and time.monotonic() >= st.cooldown_until:
            st.cooldown_until = time.monotonic() + cooldown
            by_asset: dict[str, float] = {}
            for _, usd, _, a in st.window:
                by_asset[a] = by_asset.get(a, 0) + usd
            summary = {
                "window_min": window.total_seconds() / 60, "total_usd": total, "count": len(st.window),
                "long_usd": sum(w[1] for w in st.window if w[2] == "long"),
                "short_usd": sum(w[1] for w in st.window if w[2] == "short"),
                "top": [{"asset": a, "usd": v} for a, v in sorted(by_asset.items(), key=lambda x: -x[1])[:5]],
            }
            asyncio.create_task(self._deliver(st, Message("cascade", summary=summary), 1))

    async def _worker(self, st: FeedState) -> None:
        """Event-mode: gather events for a moment, then send them as one message."""
        limit = int(st.feed.rate_limit_per_min or 30)
        while True:
            first = await st.queue.get()
            batch = [first]
            deadline = time.monotonic() + BATCH_WINDOW
            while len(batch) < BATCH_MAX:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(st.queue.get(), left))
                except asyncio.TimeoutError:
                    break
            now = time.monotonic()
            while st.sent and now - st.sent[0] > 60:
                st.sent.popleft()
            if len(st.sent) >= limit:
                st.dropped += len(batch)
                continue
            st.sent.append(now)
            note = ""
            if st.dropped:
                note, st.dropped = f" ({st.dropped} earlier events skipped by the rate limit)", 0
            await self._deliver(st, Message("events", events=[l.as_dict() for l in batch]),
                                len(batch), note)

    async def _deliver(self, st: FeedState, msg: Message, n: int, note: str = "") -> None:
        err = ""
        for attempt in range(1, RETRIES + 1):
            try:
                await send_once(st.feed.type, st.cfg, msg)
                err = ""
                break
            except SendError as exc:
                err = str(exc)
                if exc.permanent or attempt == RETRIES:
                    break
                await asyncio.sleep(exc.retry_after or 2 ** attempt)
        if err:
            log.warning("feed %r delivery failed: %s", st.feed.name, err)
        try:
            async with db.session_factory()() as s:
                s.add(FeedDelivery(feed_id=st.feed.id, ok=not err,
                                   detail=(err or f"{msg.kind}: {n} event(s)") + note))
                await s.commit()
        except Exception:
            log.exception("could not record delivery")


async def prune_deliveries() -> None:
    async with db.engine().begin() as conn:
        await conn.execute(text("""
            DELETE FROM feed_deliveries d USING (
              SELECT id, row_number() OVER (PARTITION BY feed_id ORDER BY id DESC) rn FROM feed_deliveries
            ) x WHERE d.id = x.id AND x.rn > :keep"""), {"keep": KEEP_DELIVERIES})
