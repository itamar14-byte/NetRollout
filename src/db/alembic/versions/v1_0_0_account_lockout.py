"""account lockout: users.failed_sign_ins and users.locked_until

Wrong passwords in a row (failed_sign_ins): the 5th disables a local account,
or locks the factory admin for 15 minutes (locked_until) - src/accounts/users.py.

Revision ID: v1_0_0_account_lockout
Revises: v1_0_0_baseline
Create Date: 2026-10-11 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1_0_0_account_lockout'
down_revision: Union[str, Sequence[str], None] = 'v1_0_0_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('failed_sign_ins', sa.Integer(),
                                     server_default='0', nullable=False))
    op.add_column('users', sa.Column('locked_until', sa.DateTime(timezone=True),
                                     nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'locked_until')
    op.drop_column('users', 'failed_sign_ins')
