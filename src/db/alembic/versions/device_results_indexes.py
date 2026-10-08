"""device_results indexes

Results pages a user's jobs - device results filtered by user_id and
grouped by job_id - and a job's page reads its devices by job_id alone;
without these, both read every row in scope.

Revision ID: device_results_indexes
Revises: role_user_to_operator
Create Date: 2026-10-08 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'device_results_indexes'
down_revision: Union[str, Sequence[str], None] = 'role_user_to_operator'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index('ix_device_results_user_id_job_id', 'device_results',
                    ['user_id', 'job_id'])
    op.create_index('ix_device_results_job_id', 'device_results', ['job_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_device_results_job_id', table_name='device_results')
    op.drop_index('ix_device_results_user_id_job_id', table_name='device_results')
