"""device_results.action_needed

What only a person can resolve on a device after a rollout (a save that was
refused, a commit still running, another admin's pending edits …), shown on
the Results page so it doesn't depend on someone reading the log.

Revision ID: device_results_action_needed
Revises: must_change_password
Create Date: 2026-10-02 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'device_results_action_needed'
down_revision: Union[str, Sequence[str], None] = 'must_change_password'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('device_results',
                  sa.Column('action_needed', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('device_results', 'action_needed')
