"""Binance USD-M and COIN-M perpetual liquidations (forceOrder streams).

Note: the legacy `/ws/!forceOrder@arr` path is silent and the COIN-M all-market
stream mirrors USD-M, so USD-M uses the `/market/ws` path and COIN-M subscribes
to per-symbol streams.
"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Optional

import httpx
import websockets

from .base import Collector, Emit, Liq, ms_to_dt

log = logging.getLogger(__name__)

USDM_REST = "https://fapi.binance.com/fapi/v1/exchangeInfo"
COINM_REST = "https://dapi.binance.com/dapi/v1/exchangeInfo"
USDM_WS = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
COINM_WS = "wss://dstream.binance.com/market/ws"
MAX_CONN_SECS = 23 * 3600  # Binance drops connections after 24h
QUOTES = ("USDT", "USDC", "BUSD", "USD")


def guess_base(symbol: str) -> str:
    s = symbol.split("_")[0]
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def parse_force_order(
    msg: dict, *, coinm: bool, bases: dict[str, str], contract_size: dict[str, Decimal]
) -> Optional[Liq]:
    """Normalise a forceOrder event. Returns None for anything else."""
    if msg.get("e") != "forceOrder":
        return None
    o = msg["o"]
    try:
        symbol = o["s"]
        filled = Decimal(o.get("z") or "0")
        qty = filled if filled > 0 else Decimal(o["q"])
        price = Decimal(o["ap"]) if Decimal(o.get("ap") or "0") > 0 else Decimal(o["p"])
        # A SELL order liquidates a long; a BUY order liquidates a short.
        side = "long" if o["S"] == "SELL" else "short"
        ts = ms_to_dt(o["T"])
    except (KeyError, ArithmeticError, ValueError):
        return None
    if coinm:
        size = contract_size.get(symbol, Decimal(100))
        notional = qty * size
        base_qty = notional / price if price else Decimal(0)
    else:
        notional = qty * price
        base_qty = qty
    return Liq(ts, "binance", symbol, bases.get(symbol) or guess_base(symbol), side,
               price, base_qty, notional, msg)


class BinanceUsdM(Collector):
    name = "binance-usdm"
    exchange = "binance"

    def __init__(self, emit: Emit) -> None:
        super().__init__(emit)
        self.bases: dict[str, str] = {}

    async def load(self) -> None:
        async with httpx.AsyncClient(timeout=20) as c:
            data = (await c.get(USDM_REST)).json()
        self.bases = {s["symbol"]: s["baseAsset"] for s in data["symbols"]}

    async def stream(self) -> None:
        await self.load()
        async with websockets.connect(USDM_WS, ping_interval=20, ping_timeout=20) as ws:
            self.status.connected = True
            log.info("[%s] connected (%d symbols known)", self.name, len(self.bases))
            try:
                async with asyncio.timeout(MAX_CONN_SECS):
                    async for raw in ws:
                        self.touch()
                        self._handle(raw)
            except TimeoutError:
                log.info("[%s] planned 23h reconnect", self.name)

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            liq = parse_force_order(msg, coinm=False, bases=self.bases, contract_size={})
        except (ValueError, KeyError):
            return
        if liq:
            self.event(liq)


class BinanceCoinM(Collector):
    name = "binance-coinm"
    exchange = "binance"

    def __init__(self, emit: Emit) -> None:
        super().__init__(emit)
        self.bases: dict[str, str] = {}
        self.sizes: dict[str, Decimal] = {}
        self.perps: list[str] = []

    async def load(self) -> None:
        async with httpx.AsyncClient(timeout=20) as c:
            data = (await c.get(COINM_REST)).json()
        syms = data["symbols"]
        self.bases = {s["symbol"]: s["baseAsset"] for s in syms}
        self.sizes = {s["symbol"]: Decimal(str(s["contractSize"])) for s in syms}
        self.perps = [s["symbol"].lower() for s in syms
                      if s["contractType"] == "PERPETUAL" and s["contractStatus"] == "TRADING"]

    async def stream(self) -> None:
        await self.load()
        async with websockets.connect(COINM_WS, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"method": "SUBSCRIBE", "id": 1,
                                      "params": [f"{p}@forceOrder" for p in self.perps]}))
            self.status.connected = True
            log.info("[%s] subscribed to %d perpetuals", self.name, len(self.perps))
            try:
                async with asyncio.timeout(MAX_CONN_SECS):
                    async for raw in ws:
                        self.touch()
                        try:
                            liq = parse_force_order(json.loads(raw), coinm=True,
                                                    bases=self.bases, contract_size=self.sizes)
                        except (ValueError, KeyError):
                            continue
                        if liq:
                            self.event(liq)
            except TimeoutError:
                log.info("[%s] planned 23h reconnect", self.name)
