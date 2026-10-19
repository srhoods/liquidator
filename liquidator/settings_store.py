from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting


async def get(s: AsyncSession, key: str, default: Any = None) -> Any:
    row = (await s.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return default if row is None else row.value


async def put(s: AsyncSession, key: str, value: Any) -> None:
    stmt = insert(Setting).values(key=key, value=value)
    await s.execute(stmt.on_conflict_do_update(index_elements=["key"], set_={"value": stmt.excluded.value}))
    await s.commit()
