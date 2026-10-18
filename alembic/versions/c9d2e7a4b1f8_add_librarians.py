"""add librarians: admin accounts move from auth.env into the database

If ADMIN_PASSWORD is set, the account it describes becomes the super user (its
password stored as an Argon2 hash, no email yet). migrate.sh then removes the
line from auth.env, once the upgrade has committed, so the secret no longer sits
on disk. Both run in the api container, where auth.env is visible.

Revision ID: c9d2e7a4b1f8
Revises: e5a1c0b7d2f4
Create Date: 2026-10-18 00:00:00.000000

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from argon2 import PasswordHasher

revision: str = 'c9d2e7a4b1f8'
down_revision: Union[str, None] = 'e5a1c0b7d2f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    table = op.create_table(
        'librarians',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('username', sa.String(64), nullable=False, unique=True),
        sa.Column('email', sa.String(254), unique=True),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('is_super', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('disabled_at', sa.DateTime(timezone=True)),
    )
    password = os.environ.get('ADMIN_PASSWORD')
    if password:
        op.bulk_insert(table, [{
            'username': os.environ.get('ADMIN_USERNAME') or 'admin',
            'password_hash': PasswordHasher().hash(password),
            'is_super': True,
        }])


def downgrade() -> None:
    op.drop_table('librarians')
