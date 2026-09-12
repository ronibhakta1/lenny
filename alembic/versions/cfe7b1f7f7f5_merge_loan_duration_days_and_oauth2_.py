"""merge loan_duration_days and oauth2 branches

Revision ID: cfe7b1f7f7f5
Revises: a7c3e9f1b2d4, a7c4e91d2f80
Create Date: 2026-09-12 21:49:52.686788

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cfe7b1f7f7f5'
down_revision: Union[str, None] = ('a7c3e9f1b2d4', 'a7c4e91d2f80')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
