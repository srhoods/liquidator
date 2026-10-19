from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# range -> (lookback, bucket)
RANGES = {
    "1h": (timedelta(hours=1), timedelta(minutes=5)),
    "4h": (timedelta(hours=4), timedelta(minutes=15)),
    "24h": (timedelta(hours=24), timedelta(hours=1)),
    "7d": (timedelta(days=7), timedelta(hours=1)),
}


def _f(v: Any) -> float:
    return float(v or 0)


async def dashboard(s: AsyncSession, rng: str) -> dict:
    """Totals, per-exchange series and top assets. Ranges up to 24h read raw events
    (accurate to the minute); 7d reads the hourly rollup."""
    lookback, bucket = RANGES[rng]
    since = datetime.now(timezone.utc) - lookback
    p = {"since": since, "bucket": bucket}
    if rng == "7d":
        since_h = since.replace(minute=0, second=0, microsecond=0)
        p["since"] = since_h
        tot = (await s.execute(text("""
            SELECT coalesce(sum(count),0) n, coalesce(sum(sum_usd),0) usd,
                   coalesce(sum(sum_usd) FILTER (WHERE side='long'),0) long_usd,
                   coalesce(sum(sum_usd) FILTER (WHERE side='short'),0) short_usd,
                   coalesce(max(max_usd),0) biggest
            FROM liq_hourly WHERE hour >= :since"""), p)).mappings().one()
        series = (await s.execute(text("""
            SELECT hour AS bucket, exchange, sum(sum_usd) usd FROM liq_hourly
            WHERE hour >= :since GROUP BY 1,2 ORDER BY 1"""), p)).all()
        assets = (await s.execute(text("""
            SELECT base_asset, sum(sum_usd) usd, sum(count) n FROM liq_hourly
            WHERE hour >= :since GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""), p)).all()
        largest = (await s.execute(text("""
            SELECT exchange, symbol, side, notional_usd, ts, price FROM liquidations
            WHERE ts >= :since ORDER BY notional_usd DESC LIMIT 1"""), p)).mappings().first()
    else:
        tot = (await s.execute(text("""
            SELECT count(*) n, coalesce(sum(notional_usd),0) usd,
                   coalesce(sum(notional_usd) FILTER (WHERE side='long'),0) long_usd,
                   coalesce(sum(notional_usd) FILTER (WHERE side='short'),0) short_usd,
                   coalesce(max(notional_usd),0) biggest
            FROM liquidations WHERE ts >= :since"""), p)).mappings().one()
        series = (await s.execute(text("""
            SELECT date_bin(:bucket, ts, TIMESTAMPTZ '2000-01-01') AS bucket, exchange,
                   sum(notional_usd) usd
            FROM liquidations WHERE ts >= :since GROUP BY 1,2 ORDER BY 1"""), p)).all()
        assets = (await s.execute(text("""
            SELECT base_asset, sum(notional_usd) usd, count(*) n FROM liquidations
            WHERE ts >= :since GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""), p)).all()
        largest = (await s.execute(text("""
            SELECT exchange, symbol, side, notional_usd, ts, price FROM liquidations
            WHERE ts >= :since ORDER BY notional_usd DESC LIMIT 1"""), p)).mappings().first()
    by_ex = (await s.execute(text("""
        SELECT exchange, sum(notional_usd) usd, count(*) n FROM liquidations
        WHERE ts >= :since GROUP BY 1"""), {"since": p["since"]})).all()
    return {
        "range": rng,
        "bucket_seconds": int(bucket.total_seconds()),
        "totals": {"count": int(tot["n"]), "usd": _f(tot["usd"]), "long_usd": _f(tot["long_usd"]),
                   "short_usd": _f(tot["short_usd"]), "biggest": _f(tot["biggest"])},
        "largest": ({**dict(largest), "notional_usd": _f(largest["notional_usd"]),
                     "price": _f(largest["price"]), "ts": largest["ts"].isoformat()}
                    if largest else None),
        "series": [{"t": r.bucket.isoformat(), "exchange": r.exchange, "usd": _f(r.usd)} for r in series],
        "top_assets": [{"asset": r.base_asset, "usd": _f(r.usd), "count": int(r.n)} for r in assets],
        "by_exchange": [{"exchange": r.exchange, "usd": _f(r.usd), "count": int(r.n)} for r in by_ex],
    }


COLS = "id, ts, exchange, symbol, base_asset, side, price, qty, notional_usd"


def row_dict(r) -> dict:
    return {"id": r.id, "ts": r.ts.isoformat(), "exchange": r.exchange, "symbol": r.symbol,
            "base_asset": r.base_asset, "side": r.side, "price": str(r.price.normalize()),
            "qty": str(r.qty.normalize()), "notional_usd": _f(r.notional_usd)}


async def recent(s: AsyncSession, limit: int = 60) -> list[dict]:
    rows = (await s.execute(text(
        f"SELECT {COLS} FROM liquidations ORDER BY ts DESC, id DESC LIMIT :n"), {"n": limit})).all()
    return [row_dict(r) for r in rows]


async def history(s: AsyncSession, *, since=None, until=None, exchange=None, asset=None,
                  side=None, min_usd=None, before=None, limit=100) -> list[dict]:
    """Filtered history with keyset pagination on (ts, id)."""
    where, p = ["true"], {"n": limit}
    if since:
        where.append("ts >= :since"); p["since"] = since
    if until:
        where.append("ts <= :until"); p["until"] = until
    if exchange:
        where.append("exchange = :exchange"); p["exchange"] = exchange
    if asset:
        where.append("upper(base_asset) = upper(:asset)"); p["asset"] = asset
    if side:
        where.append("side = :side"); p["side"] = side
    if min_usd:
        where.append("notional_usd >= :min_usd"); p["min_usd"] = min_usd
    if before:
        where.append("(ts, id) < (:b_ts, :b_id)"); p["b_ts"], p["b_id"] = before
    rows = (await s.execute(text(
        f"SELECT {COLS} FROM liquidations WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC, id DESC LIMIT :n"), p)).all()
    return [row_dict(r) for r in rows]
