"""v1.0.0 baseline

The whole schema as of v1.0.0, squashing the development history - the 8
early migrations (e87bf2cef6ca → 421574478cb2) and the 5 made during
packaging (must_change_password, device_results_action_needed,
role_user_to_operator, device_results_indexes, device_attributes). Databases
created by that history were stamped to this revision
(`alembic stamp --purge v1_0_0_baseline`).

From v1.0.0 on, released migrations are never edited or squashed: every schema
change is a new revision on top of this one.

Columns the development history added later are the last of their tables,
where it added them (`users.must_change_password`, `device_results.device_port`
then `device_results.action_needed`, `inventory.is_global`), so a fresh
install and a stamped database have the same schema.

Revision ID: v1_0_0_baseline
Revises:
Create Date: 2026-09-30 19:18:40.566778

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'v1_0_0_baseline'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ldap_servers',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('base_dn', sa.String(length=255), nullable=False),
        sa.Column('cn_identifier', sa.String(length=64), nullable=False),
        sa.Column('bind_type', sa.String(length=20), nullable=False),
        sa.Column('bind_dn', sa.String(length=255), nullable=True),
        sa.Column('bind_password', sa.String(length=255), nullable=True),
        sa.Column('use_ssl', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'ldap_groups',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('group_dn', sa.String(length=512), nullable=False),
        sa.Column('label', sa.String(length=128), nullable=False),
        sa.Column('role', sa.String(length=40), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('ldap_server_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['ldap_server_id'], ['ldap_servers.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'users',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        sa.Column('email', sa.String(length=120), nullable=True),
        sa.Column('full_name', sa.String(length=120), nullable=True),
        sa.Column('role', sa.String(length=40), nullable=False),
        sa.Column('position', sa.String(length=64), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('is_approved', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('otp_secret', sa.String(length=255), nullable=True),
        sa.Column('auth_type', sa.String(length=20), nullable=False),
        sa.Column('ldap_server_id', sa.Uuid(), nullable=True),
        sa.Column('must_change_password', sa.Boolean(),
                  server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(['ldap_server_id'], ['ldap_servers.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    op.create_index(op.f('ix_users_username'), 'users', ['username'],
                    unique=True)
    op.create_table(
        'audit_log',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('actor_id', sa.Uuid(), nullable=True),
        sa.Column('actor_username', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('object_type', sa.String(length=64), nullable=True),
        sa.Column('object_id', sa.Uuid(), nullable=True),
        sa.Column('object_label', sa.String(length=255), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('ip_address', sa.String(length=64), nullable=True),
        sa.Column('detail', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'],
                    unique=False)
    op.create_index(op.f('ix_audit_log_timestamp'), 'audit_log',
                    ['timestamp'], unique=False)
    op.create_table(
        'device_results',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('job_id', sa.Uuid(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=False),
        sa.Column('device_ip', sa.String(length=64), nullable=False),
        sa.Column('device_type', sa.String(length=64), nullable=False),
        sa.Column('commands_sent', sa.Integer(), nullable=False),
        sa.Column('commands_verified', sa.Integer(), nullable=True),
        sa.Column('fetched_config', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('device_port', sa.Integer(), server_default='22',
                  nullable=False),
        sa.Column('action_needed', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_device_results_user_id_job_id', 'device_results',
                    ['user_id', 'job_id'])
    op.create_index('ix_device_results_job_id', 'device_results', ['job_id'])
    op.create_table(
        'job_metadata',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('job_id', sa.Uuid(), nullable=False),
        sa.Column('commands', sa.JSON(), nullable=False),
        sa.Column('comment', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'property_definition',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('icon', sa.String(length=64), nullable=False),
        sa.Column('is_list', sa.Boolean(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', 'user_id'),
    )
    op.create_table(
        'security_profiles',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=True),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_secret', sa.String(length=255), nullable=False),
        sa.Column('enable_secret', sa.String(length=255), nullable=True),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'system_settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by', sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_table(
        'variable_mappings',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=True),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('property_name', sa.String(length=64), nullable=False),
        sa.Column('index', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token', 'user_id'),
    )
    op.create_table(
        'inventory',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=False),
        sa.Column('device_type', sa.String(length=64), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('var_maps', sa.JSON(), nullable=True),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('sec_profile_id', sa.Uuid(), nullable=True),
        sa.Column('is_global', sa.Boolean(), server_default=sa.false(),
                  nullable=False),
        sa.ForeignKeyConstraint(['sec_profile_id'], ['security_profiles.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'var_mapping_to_devices',
        sa.Column('mapping_id', sa.Uuid(), nullable=False),
        sa.Column('device_id', sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['inventory.id']),
        sa.ForeignKeyConstraint(['mapping_id'], ['variable_mappings.id']),
        sa.PrimaryKeyConstraint('mapping_id', 'device_id'),
    )
    op.create_table(
        'device_attributes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('device_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['device_id'], ['inventory.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('device_id', 'user_id', 'name'),
    )
    op.create_index(op.f('ix_device_attributes_user_id'), 'device_attributes',
                    ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema: back to an empty database."""
    op.drop_index(op.f('ix_device_attributes_user_id'), table_name='device_attributes')
    op.drop_table('device_attributes')
    op.drop_table('var_mapping_to_devices')
    op.drop_table('inventory')
    op.drop_table('variable_mappings')
    op.drop_table('system_settings')
    op.drop_table('security_profiles')
    op.drop_table('property_definition')
    op.drop_table('job_metadata')
    op.drop_index('ix_device_results_job_id', table_name='device_results')
    op.drop_index('ix_device_results_user_id_job_id', table_name='device_results')
    op.drop_table('device_results')
    op.drop_index(op.f('ix_audit_log_timestamp'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_table('users')
    op.drop_table('ldap_groups')
    op.drop_table('ldap_servers')
