"""OKX SWAP liquidation-orders channel."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Optional

import httpx
import websockets

from .base import Collector, Emit, Liq, ms_to_dt

log = logging.getLogger(__name__)

REST = "https://www.okx.com/api/v5/public/instruments"
WS = "wss://ws.okx.com:8443/ws/v5/public"
PING_EVERY = 20.0
DEAD_AFTER = 60.0


def parse_liquidation(msg: dict, instruments: dict[str, dict]) -> list[Liq]:
    """Normalise an OKX liquidation-orders push into zero or more events."""
    out: list[Liq] = []
    for d in msg.get("data") or []:
        inst_id = d.get("instId", "")
        inst = instruments.get(inst_id)
        if not inst:
            continue
        for det in d.get("details") or []:
            try:
                price = Decimal(det["bkPx"])
                contracts = Decimal(det["sz"])
                ts = ms_to_dt(det["ts"])
            except (KeyError, ArithmeticError, ValueError):
                continue
            pos = det.get("posSide")
            if pos not in ("long", "short"):  # net mode: a sell closes a long
                pos = "long" if det.get("side") == "sell" else "short"
            value = contracts * inst["ctVal"] * inst["ctMult"]
            if inst["ctValCcy"] == "USD":  # inverse: contract value is already USD
                notional, qty = value, (value / price if price else Decimal(0))
            else:
                qty, notional = value, value * price
            out.append(Liq(ts, "okx", inst_id, inst_id.split("-")[0], pos, price, qty,
                           notional, {"instId": inst_id, **det}))
    return out


class Okx(Collector):
    name = "okx"
    exchange = "okx"

    def __init__(self, emit: Emit) -> None:
        super().__init__(emit)
        self.instruments: dict[str, dict] = {}

    async def load(self) -> None:
        async with httpx.AsyncClient(timeout=20) as c:
            data = (await c.get(REST, params={"instType": "SWAP"})).json()["data"]
        self.instruments = {
            i["instId"]: {
                "ctVal": Decimal(i["ctVal"]),
                "ctMult": Decimal(i.get("ctMult") or "1"),
                "ctValCcy": i["ctValCcy"],
            }
            for i in data if i.get("ctVal")
        }

    async def stream(self) -> None:
        await self.load()
        async with websockets.connect(WS, ping_interval=None) as ws:
            await ws.send(json.dumps({"op": "subscribe",
                                      "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}))
            self.status.connected = True
            self.touch()
            log.info("[%s] subscribed (%d instruments)", self.name, len(self.instruments))
            loaded = time.monotonic()
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), PING_EVERY)
                except asyncio.TimeoutError:
                    if time.monotonic() - self.status.last_activity > DEAD_AFTER:
                        raise ConnectionError("no frames for %ds" % DEAD_AFTER)
                    await ws.send("ping")
                    continue
                self.touch()
                if raw == "pong":
                    continue
                msg = json.loads(raw)
                if "event" in msg:
                    if msg["event"] == "error":
                        raise RuntimeError(f"okx error: {msg}")
                    continue
                events = parse_liquidation(msg, self.instruments)
                if not events and msg.get("data") and time.monotonic() - loaded > 60:
                    await self.load()  # unknown instrument: new listing, refresh once
                    loaded = time.monotonic()
                    events = parse_liquidation(msg, self.instruments)
                for e in events:
                    self.event(e)
