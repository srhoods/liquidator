from __future__ import annotations

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from . import config

_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(config.async_dsn(), pool_size=5, max_overflow=5, pool_pre_ping=True)
    return _engine


def session_factory() -> async_sessionmaker:
    return async_sessionmaker(engine(), expire_on_commit=False)


ENSURE_PARTITIONS_SQL = text("""
DO $$
DECLARE m date;
BEGIN
  FOR i IN 0..2 LOOP
    m := date_trunc('month', now() AT TIME ZONE 'UTC')::date + (i || ' month')::interval;
    EXECUTE format('CREATE TABLE IF NOT EXISTS liquidations_%s PARTITION OF liquidations '
                   'FOR VALUES FROM (%L) TO (%L)',
                   to_char(m, 'YYYY_MM'), m::timestamptz, (m + interval '1 month')::timestamptz);
  END LOOP;
END $$
""")


async def ensure_partitions() -> None:
    async with engine().begin() as conn:
        await conn.execute(ENSURE_PARTITIONS_SQL)


def func_now():
    return func.now()
