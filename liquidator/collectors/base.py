from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Liq:
    """One normalised liquidation. `side` is the position that was liquidated."""

    ts: datetime
    exchange: str
    symbol: str
    base_asset: str
    side: str  # 'long' | 'short'
    price: Decimal
    qty: Decimal  # base-asset units
    notional_usd: Decimal
    raw: Optional[dict] = None

    def as_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "exchange": self.exchange,
            "symbol": self.symbol,
            "base_asset": self.base_asset,
            "side": self.side,
            "price": str(self.price),
            "qty": str(self.qty),
            "notional_usd": str(self.notional_usd),
        }


Emit = Callable[[Liq], None]


def ms_to_dt(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


@dataclass
class Status:
    connected: bool = False
    last_message: Optional[datetime] = None
    messages: int = 0
    last_error: str = ""
    # monotonic time of last frame of any kind (including pongs), for liveness
    last_activity: float = field(default_factory=time.monotonic)


class Collector:
    """Reconnecting websocket collector. Subclasses implement `stream()`."""

    name = "base"
    exchange = "base"
    min_stable = 60.0  # connection must live this long before backoff resets

    def __init__(self, emit: Emit) -> None:
        self.emit = emit
        self.status = Status()
        self._stop = asyncio.Event()

    def event(self, liq: Liq) -> None:
        self.status.messages += 1
        self.status.last_message = datetime.now(timezone.utc)
        self.emit(liq)

    def touch(self) -> None:
        self.status.last_activity = time.monotonic()

    async def stream(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    async def run(self) -> None:
        backoff, max_backoff = 1.0, 60.0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                log.info("[%s] connecting", self.name)
                await self.stream()
                log.info("[%s] stream ended cleanly, reconnecting", self.name)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.warning("[%s] stream error: %s", self.name, self.status.last_error)
                if time.monotonic() - started >= self.min_stable:
                    backoff = 1.0
                log.info("[%s] retrying in %.0fs", self.name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                self.status.connected = False

    def stop(self) -> None:
        self._stop.set()
