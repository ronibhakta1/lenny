"""Import batches (#245): persisted state for a bulk EPUB import.

SQLite covers the logic and the routes. Concurrency, scale and the migration
need Postgres and run when LENNY_TEST_PG_URL points at a throwaway database.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

SECRET = "internal-secret-for-tests"


@pytest.fixture(autouse=True)
def clean_tables():
    from lenny.core.db import Base, engine, session
    from lenny.core.bulk_import import batches  # noqa: F401
    Base.metadata.create_all(engine)
    session.remove()
    yield
    session.remove()
    with engine.begin() as c:
        c.exec_driver_sql("delete from import_rows")
        c.exec_driver_sql("delete from import_batches")


def rows(n, start=0, **extra):
    return [dict(path=f"lib/book{i}.epub", size=1000 + i, mtime=1_700_000_000_000 + i, **extra)
            for i in range(start, start + n)]


def new_batch():
    from lenny.core.bulk_import import batches as b
    return b.create_batch()["id"]


def set_state(batch_id, **fields):
    """Test helper: poke row columns directly, as the matcher/importer will."""
    from lenny.core.db import session as db
    from lenny.core.bulk_import.batches import ImportRow
    for row_id, values in fields.items():
        db.query(ImportRow).filter(ImportRow.id == int(row_id)).update(values)
    db.commit()


# ─── batches and retention ───────────────────────────────────────────────────

def test_a_new_batch_expires_in_thirty_days():
    from lenny.core.bulk_import import batches as b
    s = b.create_batch()
    left = datetime.fromisoformat(s["expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(days=29, hours=23) < left <= timedelta(days=30)
    assert s["total"] == 0 and s["counts"] == {}


def test_writes_extend_retention_but_reads_do_not():
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    bid = new_batch()
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    db.query(b.ImportBatch).update({"expires_at": soon}); db.commit()

    b.get_summary(bid); b.list_rows(bid)
    assert datetime.fromisoformat(b.get_summary(bid)["expires_at"]) == soon.replace(microsecond=soon.microsecond)

    b.upsert_rows(bid, rows(1))
    assert datetime.fromisoformat(b.get_summary(bid)["expires_at"]) > soon + timedelta(days=27)


def test_the_same_file_from_another_operating_system_is_the_same_row():
    """macOS writes é as e + combining accent; Windows may send backslashes."""
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    mac = dict(path="Bücher/Café.epub".replace("ü", "u\u0308").replace("é", "e\u0301"), size=5, mtime=7)
    win = dict(path="B\u00fccher\\Caf\u00e9.epub", size=5, mtime=7)
    assert b.upsert_rows(bid, [mac]) == {"created": 1, "existing": 0}
    assert b.upsert_rows(bid, [win]) == {"created": 0, "existing": 1}
    assert b.list_rows(bid)["rows"][0]["path"] == "B\u00fccher/Caf\u00e9.epub"


def test_expired_batch_is_gone_for_every_operation():
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    bid = new_batch()
    b.upsert_rows(bid, rows(1))
    db.query(b.ImportBatch).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}); db.commit()
    for call in (lambda: b.get_summary(bid), lambda: b.list_rows(bid),
                 lambda: b.upsert_rows(bid, rows(1)), lambda: b.delete_batch(bid),
                 lambda: b.bulk_update(bid, "skip")):
        with pytest.raises(b.ImportBatchExpired):
            call()


def test_expired_batches_and_their_rows_are_purged_on_create_and_list():
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    old = new_batch(); b.upsert_rows(old, rows(3))
    live = new_batch(); b.upsert_rows(live, rows(2))
    db.query(b.ImportBatch).filter(b.ImportBatch.id == old).update(
        {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}); db.commit()
    assert [x["id"] for x in b.list_batches()] == [live]          # list purges
    assert db.query(b.ImportBatch).count() == 1 and db.query(b.ImportRow).count() == 2
    with pytest.raises(b.ImportBatchNotFound):
        b.get_summary(old)
    db.query(b.ImportBatch).update({"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}); db.commit()
    new_batch()                                                    # create purges too
    assert db.query(b.ImportBatch).count() == 1 and db.query(b.ImportRow).count() == 0


def test_delete_batch_removes_its_rows_only():
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    a, c = new_batch(), new_batch()
    b.upsert_rows(a, rows(3)); b.upsert_rows(c, rows(2))
    b.delete_batch(a)
    assert db.query(b.ImportRow).count() == 2
    with pytest.raises(b.ImportBatchNotFound):
        b.get_summary(a)


# ─── upsert ──────────────────────────────────────────────────────────────────

def test_upsert_is_idempotent_and_never_resets_a_decision():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    assert b.upsert_rows(bid, rows(5)) == {"created": 5, "existing": 0}
    row_id = b.list_rows(bid)["rows"][0]["id"]
    b.update_row(bid, row_id, skipped=True, encrypted=True)
    assert b.upsert_rows(bid, rows(5)) == {"created": 0, "existing": 5}
    first = b.list_rows(bid)["rows"][0]
    assert first["skipped"] is True and first["encrypted"] is True
    assert b.get_summary(bid)["total"] == 5


def test_the_same_row_twice_in_one_call_is_one_row():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    assert b.upsert_rows(bid, rows(1) + rows(1)) == {"created": 1, "existing": 0}


def test_a_resent_row_refreshes_metadata_until_it_is_imported():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    b.upsert_rows(bid, rows(2, title="Old"))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(ids[1]): {"state": b.IMPORTED}})
    b.upsert_rows(bid, rows(2, title="New"))
    got = {r["id"]: r["title"] for r in b.list_rows(bid)["rows"]}
    assert got[ids[0]] == "New" and got[ids[1]] == "Old"


def test_metadata_is_cleaned_and_clamped():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    b.upsert_rows(bid, [dict(rows(1)[0], isbn="978-0-14-310542-8", title="t" * 999,
                             author="  Jane  ", year=99999, language="en", publisher=None)])
    r = b.list_rows(bid)["rows"][0]
    assert r["isbn"] == "9780143105428" and len(r["title"]) == 300
    assert r["author"] == "Jane" and r["year"] is None and r["publisher"] is None


@pytest.mark.parametrize("isbn, expected", [
    ("0143105426", "0143105426"), ("043942089x", "043942089X"), ("12345", None), ("not an isbn", None), (None, None)])
def test_isbn_cleaning(isbn, expected):
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    b.upsert_rows(bid, [dict(rows(1)[0], isbn=isbn)])
    assert b.list_rows(bid)["rows"][0]["isbn"] == expected


@pytest.mark.parametrize("bad", [
    dict(path="", size=1, mtime=1), dict(path="a" * 1025, size=1, mtime=1),
    dict(path="a", size=-1, mtime=1), dict(path="a", size=True, mtime=1),
    dict(path="a", size=1, mtime="x"), dict(path="a", size=1, mtime=1, state="matched"),
    "not a dict", dict(size=1, mtime=1),
])
def test_bad_rows_are_rejected_and_nothing_is_written(bad):
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    with pytest.raises(ValueError):
        b.upsert_rows(bid, rows(1) + [bad])
    assert b.get_summary(bid)["total"] == 0


def test_more_than_the_per_call_cap_is_rejected():
    from lenny.core.bulk_import import batches as b
    with pytest.raises(ValueError):
        b.upsert_rows(new_batch(), rows(b.MAX_ROWS_PER_CALL + 1))


def test_not_epub_rows_are_kept_for_the_report():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    b.upsert_rows(bid, [dict(rows(1)[0], state="not_epub")])
    assert b.get_summary(bid)["counts"] == {"not_epub": 1}


# ─── listing, filters, paging ────────────────────────────────────────────────

def test_keyset_paging_visits_every_row_once_in_order():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    for start in range(0, 450, 150):
        b.upsert_rows(bid, rows(150, start))
    seen, cursor = [], None
    while True:
        page = b.list_rows(bid, cursor=cursor, limit=100)
        seen += [r["id"] for r in page["rows"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 450 and seen == sorted(set(seen))


def test_limit_is_capped():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(250))
    assert len(b.list_rows(bid, limit=10_000)["rows"]) == b.MAX_PAGE


def test_filters_state_skipped_auto_picked_and_q():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    b.upsert_rows(bid, [dict(r, title=t) for r, t in zip(rows(4), ["Dune", "Emma", "100% Pure", "a_b"])])
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(ids[0]): {"state": b.MATCHED, "auto_picked": True},
                      str(ids[1]): {"state": b.MATCHED, "skipped": True}})
    listing = lambda **kw: [r["id"] for r in b.list_rows(bid, **kw)["rows"]]
    assert listing(state="matched") == ids[:2]
    assert listing(auto_picked=True) == [ids[0]]
    assert listing(skipped=True) == [ids[1]]
    assert listing(skipped=False) == [ids[0], ids[2], ids[3]]
    assert listing(q="dune") == [ids[0]]
    assert listing(q="100%") == [ids[2]]          # % is literal, not a wildcard
    assert listing(q="a_b") == [ids[3]]
    assert listing(q="a_c") == []                 # _ is literal too
    assert listing(q="book1.epub") == [ids[1]]    # path is searched


def test_summary_counts_ready_auto_picked_skipped_encrypted():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(5))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{
        str(ids[0]): {"state": b.MATCHED},
        str(ids[1]): {"state": b.MATCHED, "auto_picked": True},
        str(ids[2]): {"state": b.MATCHED, "skipped": True, "encrypted": True},
        str(ids[3]): {"state": b.NO_MATCH},
    })
    s = b.get_summary(bid)
    assert s["counts"] == {"matched": 3, "no_match": 1, "queued": 1}
    assert (s["total"], s["ready"], s["auto_picked"], s["skipped"], s["encrypted"]) == (5, 2, 1, 1, 1)


# ─── single-row updates ──────────────────────────────────────────────────────

def test_choosing_an_edition_marks_the_row_user_chosen_and_matched():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): {"state": b.NEEDS_REVIEW, "auto_picked": True, "error": "x"}})
    r = b.update_row(bid, rid, olid=555)
    assert (r["olid"], r["decision"], r["state"], r["auto_picked"], r["error"]) == (555, "user", "matched", False, None)


def test_a_row_being_uploaded_cannot_be_changed():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): {"state": b.IMPORTING}})
    with pytest.raises(b.ImportRowBusy):
        b.update_row(bid, rid, skipped=True)


def test_unknown_row_and_rows_of_other_batches_are_not_found():
    from lenny.core.bulk_import import batches as b
    a, c = new_batch(), new_batch(); b.upsert_rows(a, rows(1))
    rid = b.list_rows(a)["rows"][0]["id"]
    with pytest.raises(b.ImportRowNotFound):
        b.update_row(a, rid + 999, skipped=True)
    with pytest.raises(b.ImportRowNotFound):
        b.update_row(c, rid, skipped=True)


def test_changing_the_edition_of_an_imported_row_renames_the_item():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): {"state": b.IMPORTED, "olid": 111}})
    with patch("lenny.core.api.LennyAPI.rename_item") as rename:
        r = b.update_row(bid, rid, olid=222)
    rename.assert_called_once_with(111, 222)
    assert (r["olid"], r["state"], r["decision"]) == (222, "imported", "user")


def test_a_failed_rename_leaves_the_row_untouched():
    from lenny.core.bulk_import import batches as b
    from lenny.core.exceptions import ItemExistsError
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): {"state": b.IMPORTED, "olid": 111}})
    with patch("lenny.core.api.LennyAPI.rename_item", side_effect=ItemExistsError("exists")):
        with pytest.raises(ItemExistsError):
            b.update_row(bid, rid, olid=222)
    assert b.list_rows(bid)["rows"][0]["olid"] == 111


def test_encrypting_an_imported_row_flips_the_items_flag_too():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): {"state": b.IMPORTED, "olid": 111}})
    with patch("lenny.core.api.LennyAPI.update_item") as update:
        r = b.update_row(bid, rid, encrypted=True)
    update.assert_called_once_with(111, encrypted=True)
    assert r["encrypted"] is True
    with patch("lenny.core.api.LennyAPI.update_item") as update:
        b.update_row(bid, rid, encrypted=True)       # unchanged: no item call
    update.assert_not_called()


def test_changing_edition_and_encryption_together_applies_both():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): {"state": b.IMPORTED, "olid": 111}})
    with patch("lenny.core.api.LennyAPI.rename_item") as rename, \
         patch("lenny.core.api.LennyAPI.update_item") as update:
        r = b.update_row(bid, rid, olid=222, encrypted=True, skipped=True)
    rename.assert_called_once_with(111, 222)
    update.assert_called_once_with(222, encrypted=True)
    assert (r["olid"], r["encrypted"], r["skipped"], r["state"]) == (222, True, True, "imported")


def test_a_failed_item_update_leaves_the_row_untouched():
    from lenny.core.bulk_import import batches as b
    from lenny.core.exceptions import ItemNotFoundError
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): {"state": b.IMPORTED, "olid": 111}})
    with patch("lenny.core.api.LennyAPI.update_item", side_effect=ItemNotFoundError("gone")):
        with pytest.raises(ItemNotFoundError):
            b.update_row(bid, rid, encrypted=True)
    assert b.list_rows(bid)["rows"][0]["encrypted"] is False


def test_bulk_encrypt_flips_the_items_of_imported_rows_only():
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    from lenny.core.models import Item, FormatEnum
    for olid in (801, 802, 803):
        db.add(Item(openlibrary_edition=olid, encrypted=False, formats=FormatEnum.EPUB))
    db.commit()
    try:
        bid = new_batch(); b.upsert_rows(bid, rows(2))
        ids = [r["id"] for r in b.list_rows(bid)["rows"]]
        set_state(bid, **{str(ids[0]): {"state": b.IMPORTED, "olid": 801},
                          str(ids[1]): {"state": b.MATCHED, "olid": 802}})
        assert b.bulk_update(bid, "set_encrypted", value=True) == 2
        enc = {i.openlibrary_edition: i.encrypted for i in db.query(Item).filter(
            Item.openlibrary_edition.in_([801, 802, 803]))}
        assert enc == {801: True, 802: False, 803: False}
        b.bulk_update(bid, "set_encrypted", value=False)
        assert db.query(Item).filter(Item.openlibrary_edition == 801).one().encrypted is False
    finally:
        db.query(Item).filter(Item.openlibrary_edition.in_([801, 802, 803])).delete(synchronize_session=False)
        db.commit()


# ─── bulk actions ────────────────────────────────────────────────────────────

def test_bulk_skip_follows_the_filter_and_spares_settled_rows():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(6))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{
        str(ids[0]): {"state": b.NO_MATCH}, str(ids[1]): {"state": b.NO_MATCH},
        str(ids[2]): {"state": b.IMPORTED}, str(ids[3]): {"state": b.IMPORTING},
        str(ids[4]): {"state": b.NOT_EPUB},
    })
    assert b.bulk_update(bid, "skip", state="no_match") == 2
    assert b.bulk_update(bid, "skip") == 1                      # only the one queued row left
    skipped = {r["id"] for r in b.list_rows(bid, skipped=True)["rows"]}
    assert skipped == {ids[0], ids[1], ids[5]}
    assert b.bulk_update(bid, "unskip", state="no_match") == 2
    assert {r["id"] for r in b.list_rows(bid, skipped=True)["rows"]} == {ids[5]}


def test_bulk_skip_keeps_a_user_chosen_edition():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    b.update_row(bid, rid, olid=9)
    b.bulk_update(bid, "skip"); b.bulk_update(bid, "unskip")
    r = b.list_rows(bid)["rows"][0]
    assert (r["olid"], r["decision"], r["skipped"]) == (9, "user", False)


def test_bulk_set_encrypted_applies_to_every_matching_row_including_settled_ones():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(3))
    assert b.bulk_update(bid, "set_encrypted", value=True) == 3
    assert b.get_summary(bid)["encrypted"] == 3
    assert b.bulk_update(bid, "set_encrypted", value=False) == 3
    assert b.get_summary(bid)["encrypted"] == 0


@pytest.mark.parametrize("kwargs", [dict(action="nope"), dict(action="set_encrypted"),
                                    dict(action="set_encrypted", value="yes"), dict(action=None)])
def test_bulk_update_rejects_bad_input(kwargs):
    from lenny.core.bulk_import import batches as b
    with pytest.raises(ValueError):
        b.bulk_update(new_batch(), **kwargs)


# ─── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app_module():
    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield app


@pytest.fixture
def admin_headers():
    from lenny.core import auth
    with patch.object(auth, "ADMIN_INTERNAL_SECRET", SECRET), \
         patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None):
        yield {"X-Admin-Internal-Secret": SECRET,
               "Authorization": f"Bearer {auth.issue_admin_token()}"}


@pytest.fixture
def client(app_module, admin_headers):
    from fastapi.testclient import TestClient
    c = TestClient(app_module, headers=admin_headers)
    yield c
    c.close()


API = "/v1/api/admin/import/batches"


@pytest.mark.parametrize("method, path", [
    ("post", API), ("get", API), ("get", f"{API}/1"), ("delete", f"{API}/1"),
    ("put", f"{API}/1/rows"), ("get", f"{API}/1/rows"),
    ("patch", f"{API}/1/rows/1"), ("post", f"{API}/1/actions"),
])
def test_every_route_requires_the_admin_credentials(app_module, method, path):
    from fastapi.testclient import TestClient
    r = TestClient(app_module).request(method.upper(), path, json={})
    assert r.status_code == 403


def test_the_whole_flow_over_http(client):
    created = client.post(API)
    assert created.status_code == 201
    bid = created.json()["id"]
    assert client.get(API).json()["batches"][0]["id"] == bid

    put = client.put(f"{API}/{bid}/rows", json={"rows": rows(3, title="Dune")})
    assert put.json() == {"created": 3, "existing": 0}
    listing = client.get(f"{API}/{bid}/rows", params={"q": "dune", "limit": 2}).json()
    assert len(listing["rows"]) == 2 and listing["next_cursor"] == listing["rows"][-1]["id"]
    rid = listing["rows"][0]["id"]

    patched = client.patch(f"{API}/{bid}/rows/{rid}", json={"skipped": True, "encrypted": True})
    assert patched.json()["skipped"] is True and patched.json()["encrypted"] is True

    assert client.post(f"{API}/{bid}/actions", json={"action": "unskip"}).json() == {"updated": 1}
    s = client.get(f"{API}/{bid}").json()
    assert s["total"] == 3 and s["skipped"] == 0 and s["encrypted"] == 1

    assert client.delete(f"{API}/{bid}").status_code == 204
    assert client.get(f"{API}/{bid}").status_code == 404


def test_http_errors_are_mapped(client):
    bid = client.post(API).json()["id"]
    assert client.get(f"{API}/999999").status_code == 404
    assert client.put(f"{API}/{bid}/rows", json={"rows": "x"}).status_code == 400
    assert client.put(f"{API}/{bid}/rows", json={"rows": [{"path": ""}]}).status_code == 400
    assert client.get(f"{API}/{bid}/rows", params={"state": "bogus"}).status_code == 400
    assert client.patch(f"{API}/{bid}/rows/1", json={}).status_code == 400
    assert client.patch(f"{API}/{bid}/rows/1", json={"olid": "x"}).status_code == 400
    assert client.patch(f"{API}/{bid}/rows/1", json={"skipped": "yes"}).status_code == 400
    assert client.patch(f"{API}/{bid}/rows/12345", json={"skipped": True}).status_code == 404
    assert client.post(f"{API}/{bid}/actions", json={"action": "nope"}).status_code == 400
    assert client.post(f"{API}/{bid}/actions", json={"action": "skip", "state": "bogus"}).status_code == 400
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    db.query(b.ImportBatch).update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}); db.commit()
    assert client.get(f"{API}/{bid}").status_code == 410


# ─── Postgres only ───────────────────────────────────────────────────────────

@pytest.fixture
def pg():
    url = os.environ.get("LENNY_TEST_PG_URL")
    if not url:
        pytest.skip("set LENNY_TEST_PG_URL to a throwaway Postgres")
    from sqlalchemy import create_engine
    from lenny.core.db import Base, session
    engine = create_engine(url)
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    session.remove()
    old = session.session_factory.kw.get("bind")
    session.configure(bind=engine)
    yield engine
    session.remove(); session.configure(bind=old)
    Base.metadata.drop_all(engine); engine.dispose()


def test_concurrent_drops_of_the_same_folder_create_each_row_once(pg):
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session
    bid = b.create_batch()["id"]; session.remove()
    results, barrier = [], threading.Barrier(6)

    def worker():
        try:
            barrier.wait()
            results.append(b.upsert_rows(bid, rows(200)))
        finally:
            session.remove()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(results) == 6
    assert sum(r["created"] for r in results) == 200
    assert b.get_summary(bid)["total"] == 200


def test_fifty_thousand_rows_page_and_count_correctly(pg):
    from lenny.core.bulk_import import batches as b
    bid = b.create_batch()["id"]
    for start in range(0, 50_000, 500):
        b.upsert_rows(bid, rows(500, start))
    started = time.monotonic()
    s = b.get_summary(bid)
    assert s["total"] == 50_000 and s["counts"] == {"queued": 50_000}
    assert time.monotonic() - started < 2.0
    seen, cursor, pages = 0, None, 0
    while True:
        page = b.list_rows(bid, cursor=cursor, limit=200)
        seen += len(page["rows"]); pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == 50_000 and pages == 250
    started = time.monotonic()
    assert b.bulk_update(bid, "skip") == 50_000
    assert time.monotonic() - started < 5.0


def test_the_migration_matches_the_models(pg):
    import importlib.util
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from lenny.core.db import Base
    with pg.begin() as c:
        c.exec_driver_sql("drop table if exists import_rows, import_batches cascade")
    path = os.path.join(os.path.dirname(__file__), "..", "..", "alembic", "versions", "e5a1c0b7d2f4_add_import_batches.py")
    spec = importlib.util.spec_from_file_location("mig_e5a1", path)
    mig = importlib.util.module_from_spec(spec); spec.loader.exec_module(mig)
    with pg.begin() as c:
        ctx = MigrationContext.configure(c)
        with Operations.context(ctx):
            mig.upgrade()
        ours = {"import_batches", "import_rows"}
        diffs = [d for d in compare_metadata(ctx, Base.metadata)
                 if "import_" in repr(d)]
        assert diffs == [], diffs
        with Operations.context(ctx):
            mig.downgrade()
    with pg.connect() as c:
        left = c.exec_driver_sql(
            "select count(*) from information_schema.tables where table_name in ('import_rows','import_batches')").scalar()
    assert left == 0
