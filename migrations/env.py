from alembic import context
from sqlalchemy import create_engine

from liquidator import config
from liquidator.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(config.sync_dsn())
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
