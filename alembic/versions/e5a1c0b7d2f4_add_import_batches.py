"""add import_batches and import_rows for bulk EPUB import

A batch is one admin's drop of up to tens of thousands of EPUBs; a row is one
file. Kept 30 days from the last write (see lenny/core/bulk_import/batches.py).

Revision ID: e5a1c0b7d2f4
Revises: b3f6a1c9d4e2
Create Date: 2026-10-11 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e5a1c0b7d2f4'
down_revision: Union[str, None] = 'b3f6a1c9d4e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'import_batches',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('idx_import_batches_expires', 'import_batches', ['expires_at'])
    op.create_table(
        'import_rows',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('batch_id', sa.BigInteger, sa.ForeignKey('import_batches.id', ondelete='CASCADE'), nullable=False),
        sa.Column('row_key', sa.String(40), nullable=False),
        sa.Column('path', sa.String(1024), nullable=False),
        sa.Column('size', sa.BigInteger, nullable=False),
        sa.Column('mtime', sa.BigInteger, nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('decision', sa.String(8), nullable=False),
        sa.Column('skipped', sa.Boolean, nullable=False),
        sa.Column('encrypted', sa.Boolean, nullable=False),
        sa.Column('olid', sa.BigInteger, nullable=True),
        sa.Column('auto_picked', sa.Boolean, nullable=False),
        sa.Column('candidates_count', sa.Integer, nullable=False),
        sa.Column('confidence', sa.SmallInteger, nullable=False),
        sa.Column('match_title', sa.String(300), nullable=True),
        sa.Column('match_author', sa.String(200), nullable=True),
        sa.Column('isbn', sa.String(13), nullable=True),
        sa.Column('title', sa.String(300), nullable=True),
        sa.Column('author', sa.String(200), nullable=True),
        sa.Column('language', sa.String(16), nullable=True),
        sa.Column('publisher', sa.String(200), nullable=True),
        sa.Column('year', sa.SmallInteger, nullable=True),
        sa.Column('error', sa.String(200), nullable=True),
        sa.UniqueConstraint('batch_id', 'row_key', name='uq_import_rows_batch_key'),
    )
    op.create_index('idx_import_rows_batch_state', 'import_rows', ['batch_id', 'state', 'id'])


def downgrade() -> None:
    op.drop_index('idx_import_rows_batch_state', table_name='import_rows')
    op.drop_table('import_rows')
    op.drop_index('idx_import_batches_expires', table_name='import_batches')
    op.drop_table('import_batches')
