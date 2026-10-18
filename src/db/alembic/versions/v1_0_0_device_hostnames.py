"""device hostnames: inventory.ip and device_results.device_ip hold a name
(255), inventory.resolved_ip

A device's address is an IP or a hostname (src/rollout/address.py); a
hostname's address when it was saved goes in resolved_ip, for the "same
device" warning (src/inventory.py).

Revision ID: v1_0_0_device_hostnames
Revises: v1_0_0_personal_credentials
Create Date: 2026-10-18 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1_0_0_device_hostnames'
down_revision: Union[str, Sequence[str], None] = 'v1_0_0_personal_credentials'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('inventory', 'ip', type_=sa.String(length=255),
                    existing_type=sa.String(length=64), existing_nullable=False)
    op.add_column('inventory', sa.Column('resolved_ip', sa.String(length=64),
                                         nullable=True))
    op.alter_column('device_results', 'device_ip', type_=sa.String(length=255),
                    existing_type=sa.String(length=64), existing_nullable=False)


def downgrade() -> None:
    """Downgrade schema. A name longer than 64 is cut (a restore runs every
    downgrade over the data there; it must not fail on one)."""
    op.alter_column('device_results', 'device_ip', type_=sa.String(length=64),
                    existing_type=sa.String(length=255), existing_nullable=False,
                    postgresql_using='left(device_ip, 64)')
    op.drop_column('inventory', 'resolved_ip')
    op.alter_column('inventory', 'ip', type_=sa.String(length=64),
                    existing_type=sa.String(length=255), existing_nullable=False,
                    postgresql_using='left(ip, 64)')
