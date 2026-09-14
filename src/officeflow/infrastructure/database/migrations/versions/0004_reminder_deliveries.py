"""Add persistent reminder delivery history for deduplication and snoozing."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from officeflow.infrastructure.database import models  # noqa: F401
from officeflow.infrastructure.database.base import Base

revision: str = "0004_reminder_deliveries"
down_revision: str | None = "0003_recurrence_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["reminder_deliveries"].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "reminder_deliveries" in inspect(bind).get_table_names():
        op.drop_table("reminder_deliveries")
