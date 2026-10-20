"""verification by the configuration: device_results.config_diff,
transcript, review_outcome, review_comment, reviewed_by, reviewed_at

A push is judged by the device's configuration before and after it
(src/rollout/evidence.py): each device keeps the diff and, per command, its
verdict with the evidence and the device's reply (transcript); a device in
review is confirmed or rejected by a person (review_*).

Revision ID: v1_0_0_verification
Revises: v1_0_0_device_hostnames
Create Date: 2026-10-20 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1_0_0_verification'
down_revision: Union[str, Sequence[str], None] = 'v1_0_0_device_hostnames'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('device_results', sa.Column('config_diff', sa.Text(), nullable=True))
    op.add_column('device_results', sa.Column('transcript', sa.JSON(), nullable=True))
    op.add_column('device_results', sa.Column('review_outcome', sa.String(length=16), nullable=True))
    op.add_column('device_results', sa.Column('review_comment', sa.Text(), nullable=True))
    op.add_column('device_results', sa.Column('reviewed_by', sa.Uuid(), nullable=True))
    op.add_column('device_results', sa.Column('reviewed_at', sa.DateTime(), nullable=True))
    op.create_foreign_key('device_results_reviewed_by_fkey', 'device_results', 'users',
                          ['reviewed_by'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema. A device in review or not verified becomes partial,
    the statuses the older versions know (a restore runs every downgrade over
    the data there)."""
    op.execute("UPDATE device_results SET status = 'partial' "
               "WHERE status IN ('review', 'not verified')")
    op.drop_constraint('device_results_reviewed_by_fkey', 'device_results', type_='foreignkey')
    op.drop_column('device_results', 'reviewed_at')
    op.drop_column('device_results', 'reviewed_by')
    op.drop_column('device_results', 'review_comment')
    op.drop_column('device_results', 'review_outcome')
    op.drop_column('device_results', 'transcript')
    op.drop_column('device_results', 'config_diff')
