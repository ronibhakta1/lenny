"""add loan_duration_days to items

Revision ID: a7c3e9f1b2d4
Revises: f3a91c47b208
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7c3e9f1b2d4'
down_revision: Union[str, None] = 'f3a91c47b208'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NULL = use the global LENNY_LOAN_DURATION_DAYS setting (see
    # lenny.core.models.Loan.create).
    op.add_column('items', sa.Column('loan_duration_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('items', 'loan_duration_days')
