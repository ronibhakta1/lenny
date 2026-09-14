"""add admin loan listing indexes

Revision ID: b3f6a1c9d4e2
Revises: cfe7b1f7f7f5
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b3f6a1c9d4e2'
down_revision: Union[str, None] = 'cfe7b1f7f7f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # core/admin_loans.py's default (unfiltered) listing sorts by created_at —
    # nothing existing covers it as a leading column, so it was a full
    # sequential scan + full sort on every request regardless of table size.
    op.create_index('idx_loans_created_at', 'loans', ['created_at', 'id'])
    # status=active/overdue filters on (returned_at, due_date) together;
    # existing indexes only have returned_at as a non-leading column.
    op.create_index('idx_loans_returned_due', 'loans', ['returned_at', 'due_date'])
    # user= filter does a prefix search on patron_email_hash alone; existing
    # indexes only have it as a non-leading column behind item_id. A plain
    # btree index can't serve a LIKE 'prefix%' scan under a non-C locale (this
    # DB is en_US.utf8) — needs the pattern-ops operator class, confirmed via
    # EXPLAIN: without it, Postgres falls back to a full sequential scan.
    op.create_index(
        'idx_loans_patron_email_hash', 'loans', ['patron_email_hash'],
        postgresql_ops={'patron_email_hash': 'varchar_pattern_ops'},
    )


def downgrade() -> None:
    op.drop_index('idx_loans_patron_email_hash', table_name='loans')
    op.drop_index('idx_loans_returned_due', table_name='loans')
    op.drop_index('idx_loans_created_at', table_name='loans')
