"""add token_version to librarians so a logout can end their sessions

Revision ID: e8c1a4d7b3f6
Revises: d3a8f5c1e7b2
Create Date: 2026-10-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e8c1a4d7b3f6'
down_revision: Union[str, None] = 'd3a8f5c1e7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('librarians', sa.Column('token_version', sa.Integer, nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('librarians', 'token_version')
