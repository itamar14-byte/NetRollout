"""personal credentials: device_credentials, one security profile per user per device

Every user logs in to a device with their own security profile: a device's
profile (inventory.sec_profile_id) becomes a link from the profile's owner
(device_credentials), so nothing changes for anyone on the day of the
upgrade - the person whose profile a device used keeps using it, and other
users of a global device link their own. A composite foreign key to
security_profiles (id, user_id) makes a link to someone else's profile
impossible. inventory.user_id becomes nullable (ON DELETE SET NULL): deleting
a user keeps the global devices they created (their private ones are
deleted by src/accounts/users.delete_user).

Revision ID: v1_0_0_personal_credentials
Revises: v1_0_0_account_lockout
Create Date: 2026-10-13 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1_0_0_personal_credentials'
down_revision: Union[str, Sequence[str], None] = 'v1_0_0_account_lockout'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint('uq_security_profiles_id_user', 'security_profiles',
                                ['id', 'user_id'])
    op.create_table(
        'device_credentials',
        sa.Column('device_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('profile_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['inventory.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['profile_id', 'user_id'],
                                ['security_profiles.id', 'security_profiles.user_id'],
                                ondelete='CASCADE', name='device_credentials_profile_fkey'),
        sa.PrimaryKeyConstraint('device_id', 'user_id'),
    )
    op.create_index('ix_device_credentials_user_id', 'device_credentials', ['user_id'])
    op.create_index('ix_device_credentials_profile_id', 'device_credentials', ['profile_id'])
    # each device's profile, as its owner's link
    op.execute("INSERT INTO device_credentials (device_id, user_id, profile_id) "
               "SELECT i.id, p.user_id, p.id FROM inventory i "
               "JOIN security_profiles p ON p.id = i.sec_profile_id")
    op.drop_constraint('inventory_sec_profile_id_fkey', 'inventory', type_='foreignkey')
    op.drop_column('inventory', 'sec_profile_id')
    op.drop_constraint('inventory_user_id_fkey', 'inventory', type_='foreignkey')
    op.alter_column('inventory', 'user_id', existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key('inventory_user_id_fkey', 'inventory', 'users', ['user_id'], ['id'],
                          ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema: a device's profile is its creator's link, else any
    user's; a device without a creator goes to a user with a link, else it's
    deleted (the column is NOT NULL again)."""
    op.add_column('inventory', sa.Column('sec_profile_id', sa.Uuid(), nullable=True))
    op.execute("UPDATE inventory i SET user_id = (SELECT c.user_id FROM device_credentials c "
               "WHERE c.device_id = i.id ORDER BY c.user_id LIMIT 1) WHERE i.user_id IS NULL")
    op.execute("DELETE FROM inventory WHERE user_id IS NULL")
    op.execute("UPDATE inventory i SET sec_profile_id = coalesce("
               "(SELECT c.profile_id FROM device_credentials c "
               " WHERE c.device_id = i.id AND c.user_id = i.user_id), "
               "(SELECT c.profile_id FROM device_credentials c "
               " WHERE c.device_id = i.id ORDER BY c.user_id LIMIT 1))")
    op.create_foreign_key('inventory_sec_profile_id_fkey', 'inventory', 'security_profiles',
                          ['sec_profile_id'], ['id'])
    op.drop_constraint('inventory_user_id_fkey', 'inventory', type_='foreignkey')
    op.alter_column('inventory', 'user_id', existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key('inventory_user_id_fkey', 'inventory', 'users', ['user_id'], ['id'])
    op.drop_index('ix_device_credentials_profile_id', table_name='device_credentials')
    op.drop_index('ix_device_credentials_user_id', table_name='device_credentials')
    op.drop_table('device_credentials')
    op.drop_constraint('uq_security_profiles_id_user', 'security_profiles', type_='unique')
