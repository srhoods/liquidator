"""Fan-out of live liquidation events from Postgres LISTEN to browser websockets."""
from __future__ import annotations

import asyncio
import json
import logging

import asyncpg
from fastapi import WebSocket

from .. import config

log = logging.getLogger(__name__)


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, text: str) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_text(text), 5)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    async def listen_forever(self) -> None:
        while True:
            conn = None
            try:
                conn = await asyncpg.connect(
                    user=config.DB_USER, password=config.db_password(),
                    host=config.DB_HOST, database=config.DB_NAME)
                loop = asyncio.get_running_loop()
                await conn.add_listener(
                    "liquidation",
                    lambda _c, _pid, _ch, payload: loop.create_task(self.broadcast(payload)))
                log.info("listening for liquidation notifications")
                while not conn.is_closed():
                    await asyncio.sleep(5)
                    await conn.execute("SELECT 1")  # detect dead connection
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("listener error: %s", exc)
            finally:
                if conn and not conn.is_closed():
                    await conn.close()
            await asyncio.sleep(3)


hub = Hub()
