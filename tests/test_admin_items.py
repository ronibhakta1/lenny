"""Tests for the admin item search endpoint and its core query helper.

Mirrors tests/test_admin_loans.py: HTTP dispatch/validation against a mocked
core function, plus a real-SQLite functional test of the query logic itself.
"""

import os

import pytest
from unittest.mock import patch

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

HDRS = {"X-Admin-Internal-Secret": "x", "Authorization": "Bearer t"}


@pytest.fixture(scope="module")
def client():
    """TestClient that bypasses DB init — the listing helpers are mocked."""
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)


@pytest.fixture
def admin_ok():
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True):
        yield


# ─── Response-shape dispatch ──────────────────────────────────────────────────

def test_no_params_returns_wrapped_object(client, admin_ok):
    with patch("lenny.core.admin_items.query_items_for_admin", return_value=([{"id": 1}], 1)) as m:
        resp = client.get("/v1/api/admin/items/search", headers=HDRS)
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": 1}], "total": 1, "limit": 50, "offset": 0}
    _, kwargs = m.call_args
    assert kwargs["sort"] == "title" and kwargs["order"] == "asc"


def test_q_and_paging_flow_through(client, admin_ok):
    with patch("lenny.core.admin_items.query_items_for_admin", return_value=([{"id": 9}], 42)) as m:
        resp = client.get("/v1/api/admin/items/search?q=dune&limit=10&offset=20", headers=HDRS)
    assert resp.status_code == 200
    assert resp.json() == {"items": [{"id": 9}], "total": 42, "limit": 10, "offset": 20}
    _, kwargs = m.call_args
    assert kwargs["q"] == "dune"
    assert kwargs["offset"] == 20


# ─── Validation (no DB: guards run before the query) ──────────────────────────

def test_invalid_sort_returns_400(client, admin_ok):
    resp = client.get("/v1/api/admin/items/search?sort=nope", headers=HDRS)
    assert resp.status_code == 400


def test_invalid_order_returns_400(client, admin_ok):
    resp = client.get("/v1/api/admin/items/search?order=sideways", headers=HDRS)
    assert resp.status_code == 400


def test_negative_offset_returns_400(client, admin_ok):
    resp = client.get("/v1/api/admin/items/search?offset=-1", headers=HDRS)
    assert resp.status_code == 400


def test_requires_admin(client):
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=False):
        resp = client.get("/v1/api/admin/items/search", headers=HDRS)
    assert resp.status_code == 403


# ─── Core helper input guards ─────────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    {"sort": "nope"},
    {"order": "sideways"},
])
def test_query_items_rejects_bad_inputs(kwargs):
    from lenny.core.admin_items import query_items_for_admin
    with pytest.raises(ValueError):
        query_items_for_admin(**kwargs)


# ─── Functional: real SQLite rows, no mocking ──────────────────────────────────

@pytest.fixture
def item_rows():
    """Create the items table on the global (in-memory, TESTING) engine,
    seed a few rows, tear down after — same pattern as test_briet.py's `db`
    fixture."""
    from lenny.core.db import engine, session
    from lenny.core.models import Item, FormatEnum

    Item.__table__.create(engine, checkfirst=True)
    rows = [
        Item(openlibrary_edition=1, encrypted=False, formats=FormatEnum.EPUB,
             title="Dune", author="Frank Herbert"),
        Item(openlibrary_edition=2, encrypted=False, formats=FormatEnum.EPUB,
             title="Dune Messiah", author="Frank Herbert"),
        Item(openlibrary_edition=3, encrypted=False, formats=FormatEnum.EPUB,
             title="Foundation", author="Isaac Asimov"),
        Item(openlibrary_edition=4, encrypted=False, formats=FormatEnum.EPUB,
             title=None, author=None),  # not yet backfilled
    ]
    session.add_all(rows)
    session.commit()
    yield session
    session.rollback()
    Item.__table__.drop(engine, checkfirst=True)


def test_q_matches_title_prefix_case_insensitive(item_rows):
    from lenny.core.admin_items import query_items_for_admin
    items, total = query_items_for_admin(q="dun")
    assert total == 2
    assert {i["title"] for i in items} == {"Dune", "Dune Messiah"}


def test_q_matches_author_prefix(item_rows):
    from lenny.core.admin_items import query_items_for_admin
    items, total = query_items_for_admin(q="Isaac")
    assert total == 1
    assert items[0]["title"] == "Foundation"


def test_q_does_not_match_mid_string(item_rows):
    """Prefix-only search — the varchar_pattern_ops index can't serve a
    leading-wildcard scan, so a '%needle%' query isn't supported here."""
    from lenny.core.admin_items import query_items_for_admin
    _items, total = query_items_for_admin(q="une")
    assert total == 0


def test_no_q_returns_all_including_unbackfilled(item_rows):
    from lenny.core.admin_items import query_items_for_admin
    items, total = query_items_for_admin()
    assert total == 4
    assert any(i["title"] == "" for i in items)


def test_sort_by_title_desc(item_rows):
    from lenny.core.admin_items import query_items_for_admin
    items, _total = query_items_for_admin(q="dun", sort="title", order="desc")
    assert [i["title"] for i in items] == ["Dune Messiah", "Dune"]
