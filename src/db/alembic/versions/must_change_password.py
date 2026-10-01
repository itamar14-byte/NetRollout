"""users.must_change_password

Set for the seeded admin (and by an admin password reset): until the user
picks their own password, every page redirects to the change page. An
existing admin whose password is still the factory "admin" gets the flag
here; one that already changed it doesn't.

Revision ID: must_change_password
Revises: v1_0_0_baseline
Create Date: 2026-10-01 01:47:39.829428

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from werkzeug.security import check_password_hash


# revision identifiers, used by Alembic.
revision: str = 'must_change_password'
down_revision: Union[str, Sequence[str], None] = 'v1_0_0_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('must_change_password', sa.Boolean(),
                                     server_default=sa.false(),
                                     nullable=False))
    conn = op.get_bind()
    admin = conn.execute(sa.text(
        "select id, password_hash from users "
        "where username = 'admin' and auth_type = 'local'")).first()
    if admin and admin.password_hash and \
            check_password_hash(admin.password_hash, "admin"):
        conn.execute(sa.text("update users set must_change_password = true "
                             "where id = :id"), {"id": admin.id})


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'must_change_password')
