from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert

from .. import db
from ..collectors.base import Collector
from ..collectors.binance import BinanceCoinM, BinanceUsdM
from ..collectors.bybit import BybitCategory
from ..collectors.okx import Okx
from ..models import CollectorStatus
from ..notify.dispatcher import Dispatcher, prune_deliveries
from .scheduler import maintenance_watch, scheduled_jobs
from .writer import Writer

log = logging.getLogger(__name__)
HEARTBEAT = Path("/tmp/worker.heartbeat")


async def heartbeat(writer: Writer, collectors: list[Collector]) -> None:
    last_written = 0
    tick = -1
    while True:
        tick += 1
        HEARTBEAT.touch()
        try:
            await db.ensure_partitions()
            if tick % 60 == 0:
                await prune_deliveries()
            async with db.engine().begin() as conn:
                for c in collectors:
                    s = c.status
                    stmt = insert(CollectorStatus).values(
                        name=c.name, connected=s.connected, last_message=s.last_message,
                        messages=s.messages, last_error=s.last_error)
                    await conn.execute(stmt.on_conflict_do_update(
                        index_elements=["name"],
                        set_={"connected": stmt.excluded.connected,
                              "last_message": stmt.excluded.last_message,
                              "messages": stmt.excluded.messages,
                              "last_error": stmt.excluded.last_error,
                              "updated_at": db.func_now()}))
        except Exception:
            log.exception("heartbeat failed")
        if writer.written != last_written:
            log.info("stored %d events (queue=%d dropped=%d)", writer.written,
                     writer.queue.qsize(), writer.dropped)
            last_written = writer.written
        await asyncio.sleep(10)


async def run() -> None:
    writer = Writer()
    collectors: list[Collector] = [
        BinanceUsdM(writer.submit),
        BinanceCoinM(writer.submit),
        BybitCategory(writer.submit, "linear"),
        BybitCategory(writer.submit, "inverse"),
        Okx(writer.submit),
    ]
    dispatcher = Dispatcher()
    writer.listeners.append(dispatcher.on_events)
    tasks = [asyncio.create_task(writer.run(), name="writer"),
             asyncio.create_task(dispatcher.reload_forever(), name="feeds"),
             asyncio.create_task(maintenance_watch(writer), name="maintenance"),
             asyncio.create_task(scheduled_jobs(), name="scheduler"),
             asyncio.create_task(heartbeat(writer, collectors), name="heartbeat")]
    tasks += [asyncio.create_task(c.run(), name=c.name) for c in collectors]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker running: %s", ", ".join(c.name for c in collectors))
    await stop.wait()
    log.info("shutting down")
    for c in collectors:
        c.stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
