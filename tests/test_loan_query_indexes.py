#!/usr/bin/env python
"""
Guards the indexes that back the admin loans listing (core/admin_loans.py).

Without these, the default (unfiltered) view, the active/overdue status
filter, and the patron-search filter all fall back to a full sequential scan
+ full sort regardless of table size — confirmed via EXPLAIN ANALYZE against
100k synthetic rows (~107ms -> ~3ms after adding idx_loans_created_at alone).
This test doesn't re-measure timing (environment-dependent, flaky) — it just
makes sure a future migration can't silently drop/rename one of these
without someone noticing the query plan regressed.
"""

import pytest
from sqlalchemy import inspect, text

from lenny.core.db import engine, session as db


@pytest.fixture
def loan_indexes():
    inspector = inspect(engine)
    if "loans" not in inspector.get_table_names():
        pytest.skip("loans table not present in this test DB")
    return {ix["name"]: ix for ix in inspector.get_indexes("loans")}


def test_created_at_index_exists_for_default_sort(loan_indexes):
    ix = loan_indexes.get("idx_loans_created_at")
    assert ix is not None
    assert ix["column_names"][:2] == ["created_at", "id"]


def test_returned_due_index_exists_for_status_filters(loan_indexes):
    ix = loan_indexes.get("idx_loans_returned_due")
    assert ix is not None
    assert ix["column_names"][:2] == ["returned_at", "due_date"]


def test_patron_email_hash_index_exists_for_search_filter(loan_indexes):
    ix = loan_indexes.get("idx_loans_patron_email_hash")
    assert ix is not None
    assert ix["column_names"] == ["patron_email_hash"]


def test_patron_email_hash_index_uses_pattern_ops_on_postgres(loan_indexes):
    """A plain btree index can't serve LIKE 'prefix%' under a non-C locale —
    confirmed via EXPLAIN that without varchar_pattern_ops this silently
    falls back to a full sequential scan instead of an index range scan.
    The generic SQLAlchemy index-reflection API doesn't surface operator
    classes, so check the raw index definition directly."""
    if engine.dialect.name != "postgresql":
        pytest.skip("operator class is a PostgreSQL-specific concept")
    assert loan_indexes.get("idx_loans_patron_email_hash") is not None
    indexdef = db.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_loans_patron_email_hash'")
    ).scalar()
    assert indexdef is not None and "varchar_pattern_ops" in indexdef
