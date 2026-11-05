"""the SSH host keys devices showed: host_keys

Checked before a device is signed in to when System Settings' "Check
device host keys" is on (src/host_keys.py); accepted or refused (waiting
for an admin) per target, ip:port as connected.

Revision ID: v1_2_0_host_keys
Revises: v1_0_0_replies
Create Date: 2026-11-06 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1_2_0_host_keys'
down_revision: Union[str, Sequence[str], None] = 'v1_0_0_replies'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'host_keys',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('target', sa.String(length=300), nullable=False),
        sa.Column('key_type', sa.String(length=64), nullable=False),
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('first_seen', sa.DateTime(), nullable=False),
        sa.Column('last_seen', sa.DateTime(), nullable=False),
        sa.Column('seen_by', sa.String(length=128), nullable=False),
        sa.Column('accepted_by', sa.String(length=64), nullable=True),
        sa.Column('accepted_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('target', 'key'),
    )
    op.create_index(op.f('ix_host_keys_target'), 'host_keys', ['target'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_host_keys_target'), table_name='host_keys')
    op.drop_table('host_keys')
