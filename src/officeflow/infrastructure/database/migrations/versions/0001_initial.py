"""Create the OfficeFlow v3 baseline schema."""

from collections.abc import Sequence

from alembic import op

from officeflow.infrastructure.database import models  # noqa: F401
from officeflow.infrastructure.database.base import Base

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
