"""Optional tracking for new/reselected links; existing rows stay NULL."""

import sqlalchemy as sa
from alembic import op

revision = "0018_folder_identity"
down_revision = "0017_completion_browse_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_folder_links", sa.Column("folder_identity", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_folder_links", "folder_identity")
