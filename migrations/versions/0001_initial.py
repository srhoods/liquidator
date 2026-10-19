"""initial schema"""
revision = "0001"
down_revision = None

import sqlalchemy as sa
from alembic import op

from liquidator.models import Base


def upgrade() -> None:
    # liquidations: monthly partitions via a DEFAULT partition plus ensure_partitions()
    op.execute("""
        CREATE TABLE liquidations (
            id bigserial,
            ts timestamptz NOT NULL,
            exchange varchar(16) NOT NULL,
            symbol varchar(48) NOT NULL,
            base_asset varchar(24) NOT NULL,
            side varchar(5) NOT NULL,
            price numeric NOT NULL,
            qty numeric NOT NULL,
            notional_usd numeric NOT NULL,
            raw jsonb,
            PRIMARY KEY (id, ts),
            UNIQUE (exchange, symbol, ts, side, price, qty)
        ) PARTITION BY RANGE (ts)
    """)
    op.execute("CREATE TABLE liquidations_default PARTITION OF liquidations DEFAULT")
    op.execute("CREATE INDEX liquidations_ts_idx ON liquidations (ts DESC)")
    op.execute("CREATE INDEX liquidations_asset_idx ON liquidations (base_asset, ts)")
    op.execute("CREATE INDEX liquidations_exch_idx ON liquidations (exchange, ts)")
    op.execute("""
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
    tables = [t for n, t in Base.metadata.tables.items() if n != "liquidations"]
    Base.metadata.create_all(op.get_bind(), tables=tables)


def downgrade() -> None:
    op.execute("DROP TABLE liquidations CASCADE")
    tables = [t for n, t in Base.metadata.tables.items() if n != "liquidations"]
    Base.metadata.drop_all(op.get_bind(), tables=tables)
