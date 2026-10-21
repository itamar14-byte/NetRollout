"""what a device not judged replied: device_results.replies

Without Verify (or when the configuration couldn't be read) nothing judges
a push, but what the device answered to each command is kept - redacted - so
Results can show that a not verified device replied (engine.replied).

Revision ID: v1_0_0_replies
Revises: v1_0_0_verification
Create Date: 2026-10-21 15:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1_0_0_replies'
down_revision: Union[str, Sequence[str], None] = 'v1_0_0_verification'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('device_results', sa.Column('replies', sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('device_results', 'replies')
