"""${message}"""
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
