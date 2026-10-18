"""add a display name to librarians

Revision ID: d3a8f5c1e7b2
Revises: c9d2e7a4b1f8
Create Date: 2026-10-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd3a8f5c1e7b2'
down_revision: Union[str, None] = 'c9d2e7a4b1f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('librarians', sa.Column('name', sa.String(100)))


def downgrade() -> None:
    op.drop_column('librarians', 'name')
