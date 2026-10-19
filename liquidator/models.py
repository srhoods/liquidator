from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# NOTE: `liquidations` is a monthly-partitioned table created in the migration
# with raw SQL; this mapping is used for reads and for bulk inserts.
class Liquidation(Base):
    __tablename__ = "liquidations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(48))
    base_asset: Mapped[str] = mapped_column(String(24))
    side: Mapped[str] = mapped_column(String(5))  # 'long' | 'short' (position liquidated)
    price: Mapped[Decimal] = mapped_column(Numeric)
    qty: Mapped[Decimal] = mapped_column(Numeric)
    notional_usd: Mapped[Decimal] = mapped_column(Numeric)
    raw: Mapped[Any] = mapped_column(JSONB, nullable=True)


class LiqHourly(Base):
    __tablename__ = "liq_hourly"
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), primary_key=True)
    base_asset: Mapped[str] = mapped_column(String(24), primary_key=True)
    side: Mapped[str] = mapped_column(String(5), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    sum_usd: Mapped[Decimal] = mapped_column(Numeric)
    max_usd: Mapped[Decimal] = mapped_column(Numeric)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(10), default="viewer")  # admin | viewer
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Feed(Base):
    __tablename__ = "feeds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(16))  # discord|telegram|slack|webhook
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted JSON (urls, tokens)
    filters: Mapped[Any] = mapped_column(JSONB, default=dict)  # exchanges, assets, sides, min_usd
    mode: Mapped[str] = mapped_column(String(10), default="event")  # event | cascade
    mode_cfg: Mapped[Any] = mapped_column(JSONB, default=dict)  # window_min, threshold_usd, cooldown_min
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=30)


class FeedDelivery(Base):
    __tablename__ = "feed_deliveries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ok: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="")


class BackupTarget(Base):
    __tablename__ = "backup_targets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    type: Mapped[str] = mapped_column(String(8))  # s3 | sftp
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config_enc: Mapped[str] = mapped_column(Text)
    keep: Mapped[int] = mapped_column(Integer, default=14)


class Backup(Base):
    __tablename__ = "backups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(12), default="manual")  # manual|scheduled|pre-restore
    remotes: Mapped[Any] = mapped_column(JSONB, default=list)  # target ids holding a copy


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)


class CollectorStatus(Base):
    __tablename__ = "collector_status"
    name: Mapped[str] = mapped_column(String(48), primary_key=True)  # e.g. binance-usdm
    connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    messages: Mapped[int] = mapped_column(BigInteger, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
