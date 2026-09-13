"""add title/author to items

Revision ID: e2a5c8f1d3b7
Revises: b3f6a1c9d4e2
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e2a5c8f1d3b7'
down_revision: Union[str, None] = 'b3f6a1c9d4e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Denormalized from Open Library at add-time (core/api.py LennyAPI.add) so
    # the admin item search never has to call out to OL per request — that
    # live-lookup-per-request pattern was the source of the multi-second
    # latency found on GET /admin/loans (core/admin_loans.py). NULL until
    # scripts/backfill_item_titles.py runs for pre-existing rows.
    op.add_column('items', sa.Column('title', sa.String(length=1024), nullable=True))
    op.add_column('items', sa.Column('author', sa.String(length=512), nullable=True))
    # varchar_pattern_ops: a plain btree index can't serve LIKE 'prefix%'
    # under a non-C locale (same issue as idx_loans_patron_email_hash in
    # b3f6a1c9d4e2) — only accelerates a leading-anchor search.
    op.create_index(
        'idx_items_title', 'items', ['title'],
        postgresql_ops={'title': 'varchar_pattern_ops'},
    )
    op.create_index(
        'idx_items_author', 'items', ['author'],
        postgresql_ops={'author': 'varchar_pattern_ops'},
    )


def downgrade() -> None:
    op.drop_index('idx_items_author', table_name='items')
    op.drop_index('idx_items_title', table_name='items')
    op.drop_column('items', 'author')
    op.drop_column('items', 'title')
