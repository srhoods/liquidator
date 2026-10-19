"""Bybit v5 allLiquidation topic across linear and inverse perpetuals."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal

import httpx
import websockets

from .base import Collector, Emit, Liq, ms_to_dt

log = logging.getLogger(__name__)

REST = "https://api.bybit.com/v5/market/instruments-info"
WS = "wss://stream.bybit.com/v5/public/{category}"
SYMBOLS_PER_CONN = 300
SUB_CHUNK = 50
PING_EVERY = 20.0
DEAD_AFTER = 60.0
REFRESH_EVERY = 3600.0


def parse_all_liquidation(msg: dict, category: str, bases: dict[str, str]) -> list[Liq]:
    topic = msg.get("topic", "")
    if not topic.startswith("allLiquidation"):
        return []
    out: list[Liq] = []
    for d in msg.get("data") or []:
        try:
            symbol = d["s"]
            price = Decimal(d["p"])
            size = Decimal(d["v"])
            ts = ms_to_dt(d["T"])
            # Buy = a long position was liquidated, Sell = a short
            side = "long" if d["S"] == "Buy" else "short"
        except (KeyError, ArithmeticError, ValueError):
            continue
        if category == "inverse":  # size is in USD contracts
            notional, qty = size, (size / price if price else Decimal(0))
        else:
            qty, notional = size, size * price
        base = bases.get(symbol) or symbol.removesuffix("USDT").removesuffix("USDC").removesuffix("USD").removesuffix("PERP")
        out.append(Liq(ts, "bybit", symbol, base, side, price, qty, notional, d))
    return out


class BybitCategory(Collector):
    """One category (linear/inverse). Spawns one websocket per SYMBOLS_PER_CONN symbols."""

    exchange = "bybit"

    def __init__(self, emit: Emit, category: str) -> None:
        super().__init__(emit)
        self.category = category
        self.name = f"bybit-{category}"
        self.bases: dict[str, str] = {}
        self.groups: list[set[str]] = []
        self._conns: dict[int, websockets.ClientConnection] = {}
        self._up: set[int] = set()

    async def load(self) -> set[str]:
        want = "LinearPerpetual" if self.category == "linear" else "InversePerpetual"
        out: dict[str, str] = {}
        cursor = ""
        async with httpx.AsyncClient(timeout=20) as c:
            while True:
                params = {"category": self.category, "limit": 1000, "status": "Trading"}
                if cursor:
                    params["cursor"] = cursor
                r = (await c.get(REST, params=params)).json()["result"]
                for i in r["list"]:
                    if i.get("contractType") == want:
                        out[i["symbol"]] = i["baseCoin"]
                cursor = r.get("nextPageCursor") or ""
                if not cursor:
                    break
        self.bases = out
        return set(out)

    async def stream(self) -> None:
        symbols = sorted(await self.load())
        self.groups = [set(symbols[i:i + SYMBOLS_PER_CONN])
                       for i in range(0, len(symbols), SYMBOLS_PER_CONN)]
        log.info("[%s] %d symbols over %d connections", self.name, len(symbols), len(self.groups))
        tasks = [asyncio.create_task(self._conn(i)) for i in range(len(self.groups))]
        tasks.append(asyncio.create_task(self._refresh()))
        try:
            # any task ending (error) tears the whole category down; run() reconnects
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._conns.clear()
            self._up.clear()

    async def _subscribe(self, ws, symbols) -> None:
        syms = sorted(symbols)
        for i in range(0, len(syms), SUB_CHUNK):
            args = [f"allLiquidation.{s}" for s in syms[i:i + SUB_CHUNK]]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))

    async def _conn(self, idx: int) -> None:
        async with websockets.connect(WS.format(category=self.category), ping_interval=None) as ws:
            self._conns[idx] = ws
            await self._subscribe(ws, self.groups[idx])
            self._up.add(idx)
            self.status.connected = len(self._up) == len(self.groups)
            self.touch()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), PING_EVERY)
                except asyncio.TimeoutError:
                    if time.monotonic() - self.status.last_activity > DEAD_AFTER:
                        raise ConnectionError("no frames for %ds" % DEAD_AFTER)
                    await ws.send(json.dumps({"op": "ping"}))
                    continue
                self.touch()
                msg = json.loads(raw)
                if msg.get("op") == "subscribe" and msg.get("success") is False:
                    log.warning("[%s] subscribe failed: %s", self.name, msg.get("ret_msg"))
                    continue
                for e in parse_all_liquidation(msg, self.category, self.bases):
                    self.event(e)

    async def _refresh(self) -> None:
        """Pick up newly listed symbols without reconnecting."""
        while True:
            await asyncio.sleep(REFRESH_EVERY)
            try:
                current = await self.load()
            except Exception as exc:
                log.warning("[%s] symbol refresh failed: %s", self.name, exc)
                continue
            known = set().union(*self.groups) if self.groups else set()
            new = current - known
            if not new:
                continue
            idx = min(range(len(self.groups)), key=lambda i: len(self.groups[i]))
            self.groups[idx] |= new
            ws = self._conns.get(idx)
            if ws:
                await self._subscribe(ws, new)
            log.info("[%s] subscribed to %d new symbols", self.name, len(new))
