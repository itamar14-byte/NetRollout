"""The non-admin role is called "operator" (it was stored as "user")

The app only ever distinguishes "admin" from everything else, so this is a
rename of the stored value, in users and in the LDAP group rules.

Revision ID: role_user_to_operator
Revises: device_results_action_needed
Create Date: 2026-10-04
"""
from alembic import op

revision = "role_user_to_operator"
down_revision = "device_results_action_needed"
branch_labels = None
depends_on = None


def upgrade():
	op.execute("UPDATE users SET role = 'operator' WHERE role = 'user'")
	op.execute("UPDATE ldap_groups SET role = 'operator' WHERE role = 'user'")


def downgrade():
	op.execute("UPDATE users SET role = 'user' WHERE role = 'operator'")
	op.execute("UPDATE ldap_groups SET role = 'user' WHERE role = 'operator'")
