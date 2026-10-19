from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import timezone
from decimal import Decimal
from typing import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from ..collectors.base import Liq
from ..db import engine
from ..models import Liquidation

log = logging.getLogger(__name__)

BATCH_MAX = 500
FLUSH_EVERY = 1.0
NOTIFY_CHUNK = 15  # events per NOTIFY, keeps payloads well under the 8000 byte limit
Listener = Callable[[list[Liq]], Awaitable[None]]

ROLLUP_SQL = text("""
INSERT INTO liq_hourly (hour, exchange, base_asset, side, count, sum_usd, max_usd)
VALUES (:hour, :exchange, :base_asset, :side, :count, :sum_usd, :max_usd)
ON CONFLICT (hour, exchange, base_asset, side) DO UPDATE SET
  count = liq_hourly.count + EXCLUDED.count,
  sum_usd = liq_hourly.sum_usd + EXCLUDED.sum_usd,
  max_usd = GREATEST(liq_hourly.max_usd, EXCLUDED.max_usd)
""")


def _key(l: Liq) -> tuple:
    return (l.exchange, l.symbol, l.ts, l.side, l.price, l.qty)


class Writer:
    """Batches events from collectors into Postgres. Dedupes on the unique key,
    maintains hourly rollups and NOTIFYs the web container for live updates."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Liq] = asyncio.Queue(maxsize=100_000)
        self.running = asyncio.Event()
        self.running.set()  # cleared during restore: events buffer in memory
        self.idle = asyncio.Event()
        self.idle.set()
        self.listeners: list[Listener] = []
        self.written = 0
        self.dropped = 0

    def submit(self, liq: Liq) -> None:
        try:
            self.queue.put_nowait(liq)
        except asyncio.QueueFull:
            self.dropped += 1

    async def run(self) -> None:
        while True:
            first = await self.queue.get()
            batch = [first]
            deadline = asyncio.get_event_loop().time() + FLUSH_EVERY
            while len(batch) < BATCH_MAX:
                left = deadline - asyncio.get_event_loop().time()
                if left <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), left))
                except asyncio.TimeoutError:
                    break
            await self.running.wait()  # held in memory while a restore is running
            self.idle.clear()
            try:
                await self._write(batch)
            except Exception:
                log.exception("write failed, requeueing %d events", len(batch))
                for l in batch:
                    self.submit(l)
                await asyncio.sleep(5)
            finally:
                self.idle.set()

    async def _write(self, batch: list[Liq]) -> None:
        by_key = {_key(l): l for l in batch}
        rows = [
            {"ts": l.ts, "exchange": l.exchange, "symbol": l.symbol, "base_asset": l.base_asset,
             "side": l.side, "price": l.price, "qty": l.qty, "notional_usd": l.notional_usd,
             "raw": l.raw}
            for l in by_key.values()
        ]
        stmt = (insert(Liquidation).values(rows).on_conflict_do_nothing()
                .returning(Liquidation.exchange, Liquidation.symbol, Liquidation.ts,
                           Liquidation.side, Liquidation.price, Liquidation.qty))
        async with engine().begin() as conn:
            res = await conn.execute(stmt)
            new = [by_key[tuple(r)] for r in res.all() if tuple(r) in by_key]
            if not new:
                return
            agg: dict[tuple, list] = defaultdict(lambda: [0, Decimal(0), Decimal(0)])
            for l in new:
                hour = l.ts.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
                a = agg[(hour, l.exchange, l.base_asset, l.side)]
                a[0] += 1
                a[1] += l.notional_usd
                a[2] = max(a[2], l.notional_usd)
            await conn.execute(ROLLUP_SQL, [
                {"hour": k[0], "exchange": k[1], "base_asset": k[2], "side": k[3],
                 "count": v[0], "sum_usd": v[1], "max_usd": v[2]} for k, v in agg.items()])
            for i in range(0, len(new), NOTIFY_CHUNK):
                payload = json.dumps([l.as_dict() for l in new[i:i + NOTIFY_CHUNK]])
                await conn.execute(text("SELECT pg_notify('liquidation', :p)"), {"p": payload})
        self.written += len(new)
        for fn in self.listeners:
            try:
                await fn(new)
            except Exception:
                log.exception("listener failed")
