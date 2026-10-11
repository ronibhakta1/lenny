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
    assert b.upsert_rows(bid, [mac]) == {"created": 1, "existing": 0, "existing_states": {}}
    assert b.upsert_rows(bid, [win]) == {"created": 0, "existing": 1, "existing_states": {"queued": 1}}
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
    assert b.upsert_rows(bid, rows(5)) == {"created": 5, "existing": 0, "existing_states": {}}
    row_id = b.list_rows(bid)["rows"][0]["id"]
    b.update_row(bid, row_id, skipped=True, encrypted=True)
    assert b.upsert_rows(bid, rows(5)) == {"created": 0, "existing": 5, "existing_states": {"queued": 5}}
    first = b.list_rows(bid)["rows"][0]
    assert first["skipped"] is True and first["encrypted"] is True
    assert b.get_summary(bid)["total"] == 5


def test_the_same_row_twice_in_one_call_is_one_row():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    assert b.upsert_rows(bid, rows(1) + rows(1)) == {"created": 1, "existing": 0, "existing_states": {}}


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


# ─── removing rows ───────────────────────────────────────────────────────────

def test_delete_row_removes_it_and_updates_the_counts():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(3))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    summary = b.delete_row(bid, ids[1])
    assert summary["total"] == 2 and summary["counts"] == {"queued": 2}
    assert [r["id"] for r in b.list_rows(bid)["rows"]] == [ids[0], ids[2]]
    with pytest.raises(b.ImportRowNotFound):
        b.delete_row(bid, ids[1])                                  # already gone: 404


def test_delete_row_refuses_an_importing_row_but_removes_an_imported_one():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(2))
    a, c = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(a): dict(state="importing"), str(c): dict(state="imported", olid=5)})
    with pytest.raises(b.ImportRowBusy):
        b.delete_row(bid, a)
    assert b.delete_row(bid, c)["counts"] == {"importing": 1}


def test_delete_row_refreshes_retention_and_stays_inside_its_batch():
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    one, two = new_batch(), new_batch()
    b.upsert_rows(one, rows(1)); b.upsert_rows(two, rows(1))
    row_two = b.list_rows(two)["rows"][0]["id"]
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    db.query(b.ImportBatch).filter(b.ImportBatch.id == one).update({"expires_at": soon}); db.commit()
    row_one = b.list_rows(one)["rows"][0]["id"]
    with pytest.raises(b.ImportRowNotFound):
        b.delete_row(one, row_two)                                  # another batch's row
    b.delete_row(one, row_one)
    assert datetime.fromisoformat(b.get_summary(one)["expires_at"]) > soon + timedelta(days=27)
    assert b.get_summary(two)["total"] == 1


def test_a_removed_file_dropped_again_is_a_fresh_row():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1, title="Dune"))
    old = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(old): dict(state="matched", olid=5, decision="user")})
    b.delete_row(bid, old)
    assert b.upsert_rows(bid, rows(1, title="Dune")) == {"created": 1, "existing": 0, "existing_states": {}}
    new = b.list_rows(bid)["rows"][0]
    assert new["state"] == "queued" and new["olid"] is None and new["decision"] == "auto"   # nothing carried over


def test_bulk_remove_takes_the_filtered_rows_but_not_importing_ones():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(5))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    for i in ids[:3]:
        b.update_row(bid, i, skipped=True)
    set_state(bid, **{str(ids[0]): dict(state="importing")})
    assert b.bulk_update(bid, "remove", skipped=True) == 2          # the importing one stays
    assert sorted(r["id"] for r in b.list_rows(bid)["rows"]) == [ids[0], ids[3], ids[4]]
    assert b.bulk_update(bid, "remove", state="queued") == 2


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
    ("patch", f"{API}/1/rows/1"), ("delete", f"{API}/1/rows/1"), ("post", f"{API}/1/actions"), ("post", f"{API}/1/sync"),
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
    assert put.json() == {"created": 3, "existing": 0, "existing_states": {}}
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


def test_remove_over_http(client):
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path=f"{i}.epub", size=i + 1, mtime=1) for i in range(3)]})
    ids = [r["id"] for r in client.get(f"{API}/{bid}/rows").json()["rows"]]
    r = client.delete(f"{API}/{bid}/rows/{ids[0]}")
    assert r.status_code == 200 and r.json()["total"] == 2 and r.json()["counts"] == {"queued": 2}
    assert client.delete(f"{API}/{bid}/rows/{ids[0]}").status_code == 404
    client.patch(f"{API}/{bid}/rows/{ids[1]}", json={"skipped": True})
    assert client.post(f"{API}/{bid}/actions", json={"action": "remove", "skipped": True}).json() == {"updated": 1}
    assert client.get(f"{API}/{bid}").json()["total"] == 1


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
    # Numbered pages: a deep page and the total, with and without a search, stay quick.
    started = time.monotonic()
    deep = b.list_rows(bid, offset=49_950, limit=50)
    assert len(deep["rows"]) == 50 and deep["total"] == 50_000 and deep["next_cursor"] is None
    searched = b.list_rows(bid, offset=0, limit=50, q="book4999")        # book4999, book49990..49999
    assert searched["total"] == 11 and len(searched["rows"]) == 11
    assert time.monotonic() - started < 3.0
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


# ─── staging: the file lives on the server, so nothing has to stay connected ──

def epub_upload(data=None):
    import io, zipfile
    from starlette.datastructures import Headers, UploadFile
    if data is None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            z.writestr("META-INF/container.xml", "<container/>")
        data = buf.getvalue()
    return UploadFile(filename="b.epub", file=io.BytesIO(data), size=len(data),
                      headers=Headers({"content-type": "application/epub+zip"}))


@pytest.fixture
def fake_s3():
    """Stands in for the object store: remembers what was uploaded and deleted."""
    from unittest.mock import MagicMock
    store = MagicMock()
    store.BOOKSHELF_BUCKET = "bookshelf"
    store.objects = set()
    store.upload_fileobj.side_effect = lambda f, bucket, key, **k: store.objects.add(key)
    store.delete_object.side_effect = lambda Bucket, Key: store.objects.discard(Key)
    store.get_keys.side_effect = lambda prefix="", **k: [o for o in sorted(store.objects) if o.startswith(prefix)]
    with patch("lenny.core.bulk_import.staging.s3", store):
        yield store


def matched_rows(n):
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(n))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(i): dict(state="matched", olid=100 + i) for i in ids})
    return bid, ids


def test_staging_keeps_the_file_and_marks_the_row(fake_s3):
    from lenny.core.bulk_import import batches as b, staging
    bid, ids = matched_rows(2)
    row = staging.stage(bid, ids[0], epub_upload())
    assert row["staged"] is True and fake_s3.objects == {f"import-staging/{bid}/{ids[0]}.epub"}
    summary = b.get_summary(bid)
    assert summary["staged"] == 1 and summary["unstaged"] == 1           # one more still to send
    assert staging.stage(bid, ids[0], epub_upload())["staged"] is True    # idempotent
    assert len(fake_s3.objects) == 1


def test_staging_checks_the_file_before_storing_anything(fake_s3):
    from lenny.core.bulk_import import staging
    from lenny.core.exceptions import InvalidFileError
    bid, ids = matched_rows(1)
    with pytest.raises(InvalidFileError):
        staging.stage(bid, ids[0], epub_upload(b"%PDF-1.4 not an epub"))
    assert fake_s3.objects == set() and fake_s3.upload_fileobj.call_count == 0


@pytest.mark.parametrize("fields", [dict(state="queued"), dict(state="no_match"), dict(skipped=True), dict(state="imported")])
def test_only_matched_unskipped_rows_can_be_staged(fake_s3, fields):
    from lenny.core.bulk_import import batches as b, staging
    bid, ids = matched_rows(1)
    set_state(bid, **{str(ids[0]): fields})
    with pytest.raises(ValueError):
        staging.stage(bid, ids[0], epub_upload())
    with pytest.raises(b.ImportRowNotFound):
        staging.stage(bid, ids[0] + 99, epub_upload())
    assert fake_s3.objects == set()


def test_a_failed_store_leaves_the_row_unstaged(fake_s3):
    from lenny.core.bulk_import import batches as b, staging
    from lenny.core.exceptions import S3UploadError
    bid, ids = matched_rows(1)
    fake_s3.upload_fileobj.side_effect = RuntimeError("garage down")
    with pytest.raises(S3UploadError):
        staging.stage(bid, ids[0], epub_upload())
    assert b.list_rows(bid)["rows"][0]["staged"] is False


def test_a_row_removed_while_its_file_uploads_leaves_no_orphan(fake_s3):
    from lenny.core.bulk_import import batches as b, staging
    from lenny.core.db import session as db
    bid, ids = matched_rows(1)
    def upload_then_row_vanishes(f, bucket, key, **k):
        fake_s3.objects.add(key)
        db.query(b.ImportRow).filter(b.ImportRow.id == ids[0]).delete(); db.commit()
    fake_s3.upload_fileobj.side_effect = upload_then_row_vanishes
    with pytest.raises(b.ImportRowNotFound):
        staging.stage(bid, ids[0], epub_upload())
    assert fake_s3.objects == set()


def test_staged_files_go_with_their_row_batch_and_expiry(fake_s3):
    from datetime import datetime, timedelta, timezone
    from lenny.core.bulk_import import batches as b, staging
    from lenny.core.db import session as db
    one, a = matched_rows(3)
    for i in a:
        staging.stage(one, i, epub_upload())
    assert len(fake_s3.objects) == 3
    b.delete_row(one, a[0])                                                  # one row
    assert len(fake_s3.objects) == 2
    b.bulk_update(one, "remove", state="matched")                            # a filter (staged rows included)
    assert fake_s3.objects == set()
    two, c = matched_rows(2)
    for i in c:
        staging.stage(two, i, epub_upload())
    b.delete_batch(two)                                                      # the whole batch
    assert fake_s3.objects == set()
    three, d = matched_rows(1)
    staging.stage(three, d[0], epub_upload())
    db.query(b.ImportBatch).filter(b.ImportBatch.id == three).update(
        {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)}); db.commit()
    b.list_batches()                                                         # lazy purge
    assert fake_s3.objects == set()


def test_cleanup_failures_never_break_a_delete(fake_s3):
    from lenny.core.bulk_import import batches as b, staging
    bid, ids = matched_rows(2)
    staging.stage(bid, ids[0], epub_upload())
    fake_s3.delete_object.side_effect = RuntimeError("garage down")
    fake_s3.get_keys.side_effect = RuntimeError("garage down")
    assert b.delete_row(bid, ids[0])["total"] == 1
    b.delete_batch(bid)


def test_stage_over_http(client, fake_s3):
    import io
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=3, mtime=1)]})
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    files = lambda data: {"file": ("a.epub", io.BytesIO(data), "application/epub+zip")}
    assert client.put(f"{API}/{bid}/rows/{rid}/staged", files=files(b"abc")).status_code == 400   # not matched yet
    client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 101})
    assert client.put(f"{API}/{bid}/rows/{rid}/staged", files=files(b"%PDF nope")).status_code == 400
    ok = client.put(f"{API}/{bid}/rows/{rid}/staged", files=files(epub_upload().file.read()))
    assert ok.status_code == 200 and ok.json()["staged"] is True
    assert client.get(f"{API}/{bid}").json()["staged"] == 1
    assert client.put(f"{API}/{bid}/rows/999/staged", files=files(b"x")).status_code == 404


def test_rows_can_be_filtered_by_whether_their_file_is_on_the_server(client):
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path=f"{i}.epub", size=i + 1, mtime=1) for i in range(4)]})
    ids = [r["id"] for r in client.get(f"{API}/{bid}/rows").json()["rows"]]
    set_state(bid, **{str(ids[0]): dict(staged=True), str(ids[1]): dict(staged=True)})
    got = lambda **p: [r["id"] for r in client.get(f"{API}/{bid}/rows", params=p).json()["rows"]]
    assert got(staged="true") == ids[:2] and got(staged="false") == ids[2:] and len(got()) == 4
    set_state(bid, **{str(i): dict(state="matched") for i in ids})
    assert got(staged="false", state="matched") == ids[2:]


# ─── numbered pages ──────────────────────────────────────────────────────────

def test_offset_pages_are_stable_and_match_the_cursor_order():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(23))
    ids = [r["id"] for r in b.list_rows(bid, limit=200)["rows"]]
    pages = [[r["id"] for r in b.list_rows(bid, offset=o, limit=10)["rows"]] for o in (0, 10, 20)]
    assert pages[0] + pages[1] + pages[2] == ids and [len(p) for p in pages] == [10, 10, 3]
    assert b.list_rows(bid, offset=30, limit=10)["rows"] == []                  # past the end: empty, not an error
    first = b.list_rows(bid, offset=0, limit=10)
    assert first["total"] == 23 and first["next_cursor"] == pages[0][-1]


def test_total_counts_the_filtered_rows_ignoring_paging_and_is_off_unless_asked():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(12, title="Dune")); b.upsert_rows(bid, rows(5, start=12, title="Emma"))
    ids = [r["id"] for r in b.list_rows(bid, limit=200)["rows"]]
    set_state(bid, **{str(i): dict(state="matched", staged=i in ids[:2]) for i in ids[:7]})
    assert b.list_rows(bid)["total"] is None                                    # cursor passes pay for no count
    assert b.list_rows(bid, with_total=True, limit=3)["total"] == 17
    assert b.list_rows(bid, offset=0, state="matched")["total"] == 7
    assert b.list_rows(bid, offset=0, q="emma", limit=2)["total"] == 5
    assert b.list_rows(bid, offset=0, q="DUNE", state="matched", limit=2)["total"] == 7
    assert b.list_rows(bid, offset=0, staged=True)["total"] == 2
    assert b.list_rows(bid, offset=0, q="nothing")["total"] == 0


def test_offset_and_cursor_do_not_mix_and_bad_offsets_are_refused():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(3))
    with pytest.raises(ValueError):
        b.list_rows(bid, offset=1, cursor=1)
    for bad in (-1, True, "2", 1.5):
        with pytest.raises(ValueError):
            b.list_rows(bid, offset=bad)


def test_offset_and_total_over_http(client):
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path=f"b{i}.epub", size=i + 1, mtime=1, title="Dune" if i < 6 else "Emma") for i in range(9)]})
    page = client.get(f"{API}/{bid}/rows", params={"offset": 4, "limit": 4}).json()
    assert len(page["rows"]) == 4 and page["total"] == 9 and page["next_cursor"] is not None
    assert client.get(f"{API}/{bid}/rows", params={"offset": 0, "q": "emma"}).json()["total"] == 3
    assert client.get(f"{API}/{bid}/rows", params={"total": "true", "limit": 2}).json()["total"] == 9
    assert client.get(f"{API}/{bid}/rows").json()["total"] is None
    assert client.get(f"{API}/{bid}/rows", params={"offset": -1}).status_code == 400
    assert client.get(f"{API}/{bid}/rows", params={"offset": 1, "cursor": 2}).status_code == 400


def test_remove_takes_a_list_of_ticked_rows_and_nothing_else_does():
    from lenny.core.bulk_import import batches as b
    one, two = new_batch(), new_batch()
    b.upsert_rows(one, rows(6)); b.upsert_rows(two, rows(2))
    ids = [r["id"] for r in b.list_rows(one)["rows"]]
    other = b.list_rows(two)["rows"][0]["id"]
    set_state(one, **{str(ids[0]): dict(state="importing")})
    assert b.bulk_update(one, "remove", row_ids=[ids[0], ids[1], ids[2], other, 99999]) == 2   # importing, foreign and unknown ids ignored
    assert sorted(r["id"] for r in b.list_rows(one)["rows"]) == [ids[0], ids[3], ids[4], ids[5]]
    assert b.get_summary(two)["total"] == 2
    set_state(one, **{str(ids[3]): dict(state="matched")})
    assert b.bulk_update(one, "remove", row_ids=[ids[3], ids[4]], state="matched") == 1         # combined with a filter
    for bad in ([], "1", [True], [1.5], list(range(501))):
        with pytest.raises(ValueError):
            b.bulk_update(one, "remove", row_ids=bad)
    with pytest.raises(ValueError):
        b.bulk_update(one, "skip", row_ids=[ids[5]])


def test_remove_by_ids_over_http(client):
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path=f"r{i}.epub", size=i + 1, mtime=1) for i in range(5)]})
    ids = [r["id"] for r in client.get(f"{API}/{bid}/rows").json()["rows"]]
    assert client.post(f"{API}/{bid}/actions", json={"action": "remove", "row_ids": ids[:3]}).json() == {"updated": 3}
    assert client.get(f"{API}/{bid}").json()["total"] == 2
    assert client.post(f"{API}/{bid}/actions", json={"action": "remove", "row_ids": []}).status_code == 400
    assert client.post(f"{API}/{bid}/actions", json={"action": "skip", "row_ids": ids}).status_code == 400


def test_dropping_the_same_folder_again_says_what_was_already_there():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(6))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(i): dict(state="imported", olid=100 + i) for i in ids[:3]},
              **{str(i): dict(state="matched", olid=200 + i) for i in ids[3:5]})
    again = b.upsert_rows(bid, rows(8))                                          # the same six plus two new files
    # imported files come back as new rows (their old rows stay in the history); the rest are recognised
    assert again == {"created": 5, "existing": 3, "existing_states": {"matched": 2, "queued": 1}}
    assert len(b.list_rows(bid, state="imported")["rows"]) == 3                  # the history is untouched


# ─── loaded vs imported (history) ────────────────────────────────────────────

def history_batch(n_loaded=3, n_imported=4):
    """A batch with some imported rows (importing at different times) and some still loaded."""
    from datetime import datetime, timedelta, timezone
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(n_loaded + n_imported))
    ids = [r["id"] for r in b.list_rows(bid, limit=200)["rows"]]
    base = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    for k, i in enumerate(ids[:n_imported]):
        # ids[0] oldest; the last two share a timestamp, so the id must break the tie
        when = base + timedelta(hours=min(k, n_imported - 2))
        set_state(bid, **{str(i): dict(state="imported", olid=100 + i, imported_at=when)})
    return bid, ids[:n_imported], ids[n_imported:]


def test_exclude_state_lists_everything_but_that_state_and_totals_agree():
    from lenny.core.bulk_import import batches as b
    bid, imported, loaded = history_batch()
    page = b.list_rows(bid, exclude_state="imported", offset=0, limit=50)
    assert [r["id"] for r in page["rows"]] == loaded and page["total"] == 3
    assert b.list_rows(bid, exclude_state="imported", q="book", offset=0)["total"] == 3
    assert b.list_rows(bid, exclude_state="imported", state="queued", offset=0)["total"] == 3
    assert b.list_rows(bid, exclude_state="queued", state="queued", offset=0)["total"] == 0
    assert b.list_rows(bid, exclude_state="imported", staged=False, offset=1, limit=1)["rows"][0]["id"] == loaded[1]
    with pytest.raises(ValueError):
        b.list_rows(bid, exclude_state="bogus")


def test_history_is_newest_first_with_stable_pages_and_a_timestamp():
    from lenny.core.bulk_import import batches as b
    bid, imported, loaded = history_batch(n_imported=5)
    rows_ = b.list_rows(bid, state="imported", order="imported_at_desc", limit=200)["rows"]
    assert [r["id"] for r in rows_] == [imported[4], imported[3], imported[2], imported[1], imported[0]]   # tie broken by id, desc
    assert all(r["imported_at"] is not None for r in rows_)
    paged = []
    for offset in (0, 2, 4):
        paged += [r["id"] for r in b.list_rows(bid, state="imported", order="imported_at_desc", offset=offset, limit=2)["rows"]]
    assert paged == [r["id"] for r in rows_]
    assert all(r["imported_at"] is None for r in b.list_rows(bid, exclude_state="imported")["rows"])
    with pytest.raises(ValueError):
        b.list_rows(bid, order="by_vibes")
    with pytest.raises(ValueError):
        b.list_rows(bid, order="imported_at_desc", cursor=3)


def test_start_over_clears_the_loaded_rows_and_keeps_the_history():
    from lenny.core.bulk_import import batches as b
    bid, imported, loaded = history_batch()
    assert b.bulk_update(bid, "remove", exclude_state="imported") == 3
    assert [r["id"] for r in b.list_rows(bid)["rows"]] == imported
    assert b.get_summary(bid)["counts"] == {"imported": 4}
    assert b.bulk_update(bid, "remove", exclude_state="imported") == 0                  # nothing left to clear
    with pytest.raises(ValueError):
        b.bulk_update(bid, "remove", exclude_state="bogus")


def test_exclude_state_works_for_every_action():
    from lenny.core.bulk_import import batches as b
    bid, imported, loaded = history_batch()
    assert b.bulk_update(bid, "set_encrypted", value=True, exclude_state="imported") == 3
    got = {r["id"]: r for r in b.list_rows(bid)["rows"]}
    assert all(got[i]["encrypted"] for i in loaded) and not any(got[i]["encrypted"] for i in imported)


def test_loading_and_clearing_never_touch_imported_rows_and_history_keeps_the_batch_alive():
    from datetime import datetime, timedelta, timezone
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    bid, imported, loaded = history_batch()
    b.upsert_rows(bid, rows(2, start=100))                                       # new files arrive
    b.delete_row(bid, loaded[0])
    b.bulk_update(bid, "remove", state="queued")
    assert [r["id"] for r in b.list_rows(bid, state="imported")["rows"]] == imported
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    db.query(b.ImportBatch).filter(b.ImportBatch.id == bid).update({"expires_at": soon}); db.commit()
    b.upsert_rows(bid, rows(1, start=200))                                       # any change slides retention for all rows
    assert datetime.fromisoformat(b.get_summary(bid)["expires_at"]) > soon + timedelta(days=27)


def test_history_and_exclude_over_http(client):
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path=f"h{i}.epub", size=i + 1, mtime=1) for i in range(4)]})
    ids = [r["id"] for r in client.get(f"{API}/{bid}/rows").json()["rows"]]
    set_state(bid, **{str(ids[0]): dict(state="imported", imported_at=datetime(2026, 10, 2, tzinfo=timezone.utc)),
                      str(ids[1]): dict(state="imported", imported_at=datetime(2026, 10, 3, tzinfo=timezone.utc))})
    hist = client.get(f"{API}/{bid}/rows", params={"state": "imported", "order": "imported_at_desc", "offset": 0}).json()
    assert [r["id"] for r in hist["rows"]] == [ids[1], ids[0]] and hist["total"] == 2
    assert hist["rows"][0]["imported_at"].startswith("2026-10-03")
    loaded = client.get(f"{API}/{bid}/rows", params={"exclude_state": "imported", "offset": 0}).json()
    assert loaded["total"] == 2 and all(r["state"] != "imported" for r in loaded["rows"])
    assert client.get(f"{API}/{bid}/rows", params={"exclude_state": "bogus"}).status_code == 400
    assert client.get(f"{API}/{bid}/rows", params={"order": "x"}).status_code == 400
    assert client.post(f"{API}/{bid}/actions", json={"action": "remove", "exclude_state": "imported"}).json() == {"updated": 2}
    assert client.get(f"{API}/{bid}").json()["counts"] == {"imported": 2}


def test_choosing_another_edition_replaces_what_the_row_shows():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1, title="Game Show Zine"))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): dict(state="matched", olid=5, auto_picked=True, confidence=76, candidates_count=4,
                                    match_title="Game Show Zine", match_author="Old Author", match_year=1999, staged=True)})
    out = b.update_row(bid, rid, olid=9, match={"title": "Defector Weekly Digest", "author": "Tom Ley, Diana Moskovitz", "year": 2026})
    assert out["olid"] == 9 and out["state"] == "matched" and out["decision"] == "user" and out["auto_picked"] is False
    assert (out["match_title"], out["match_author"], out["match_year"]) == ("Defector Weekly Digest", "Tom Ley, Diana Moskovitz", 2026)
    assert out["confidence"] == 0 and out["candidates_count"] == 4 and out["staged"] is True         # the file stays valid
    again = b.list_rows(bid)["rows"][0]                                                              # and the list agrees
    assert (again["olid"], again["match_title"], again["match_year"]) == (9, "Defector Weekly Digest", 2026)
    # a choice without facts clears the old ones rather than leaving the wrong title behind
    out = b.update_row(bid, rid, olid=11)
    assert out["olid"] == 11 and out["match_title"] is None and out["match_author"] is None and out["match_year"] is None
    # the same edition sent again fills in details an older choice never stored (a row chosen before they were kept)
    out = b.update_row(bid, rid, olid=11, match={"title": "Filled In", "author": "A", "year": 2026})
    assert (out["match_title"], out["match_author"], out["match_year"], out["decision"]) == ("Filled In", "A", 2026, "user")
    # but it never overwrites details the row already has
    out = b.update_row(bid, rid, olid=11, match={"title": "Different"})
    assert out["match_title"] == "Filled In"


def test_other_changes_to_a_row_keep_its_match_details():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): dict(state="matched", olid=5, match_title="Keep Me", match_year=2001)})
    out = b.update_row(bid, rid, encrypted=True)
    assert out["match_title"] == "Keep Me" and out["match_year"] == 2001 and out["encrypted"] is True


# ─── the saved list vs the live library ──────────────────────────────────────

@pytest.fixture
def library():
    """The real items table (SQLite), cleaned up after."""
    from lenny.core.cache import CacheEntry
    from lenny.core.db import Base, engine, session
    from lenny.core.models import Item
    Base.metadata.create_all(engine)
    session.remove()
    yield session
    session.rollback()
    session.query(Item).delete(); session.query(CacheEntry).delete(); session.commit()
    session.remove()


def put_in_library(db, *olids):
    from lenny.core.models import FormatEnum, Item
    for o in olids:
        db.add(Item(openlibrary_edition=o, formats=FormatEnum.EPUB))
    db.commit()


def test_a_duplicate_whose_book_was_deleted_becomes_importable_again(library):
    from lenny.core.bulk_import import batches as b
    from lenny.core.models import Item
    bid = new_batch(); b.upsert_rows(bid, rows(3))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    put_in_library(library, 101, 102)
    set_state(bid, **{str(ids[0]): dict(state="duplicate", olid=101), str(ids[1]): dict(state="duplicate", olid=102),
                      str(ids[2]): dict(state="duplicate", olid=103, error="x")})        # 103 was never/no longer in the library
    out = b.reconcile(bid)
    assert out["reverted"] == 1
    got = {r["id"]: r for r in b.list_rows(bid)["rows"]}
    assert [got[i]["state"] for i in ids] == ["duplicate", "duplicate", "matched"] and got[ids[2]]["error"] is None
    library.query(Item).filter(Item.openlibrary_edition == 101).delete(); library.commit()   # the admin deletes a book
    assert b.reconcile(bid)["reverted"] == 1
    assert {r["id"]: r["state"] for r in b.list_rows(bid)["rows"]}[ids[0]] == "matched"
    assert b.reconcile(bid)["reverted"] == 0                                                  # idempotent


def test_dropping_files_again_after_deleting_their_books_loads_them(library):
    """The reported case: import, delete the books from the library, drop the same folder again."""
    from lenny.core.bulk_import import batches as b
    from lenny.core.models import Item
    bid = new_batch(); b.upsert_rows(bid, rows(4, title="Dune"))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    put_in_library(library, 100 + ids[0], 100 + ids[1])
    set_state(bid, **{str(ids[0]): dict(state="duplicate", olid=100 + ids[0]),
                      str(ids[1]): dict(state="imported", olid=100 + ids[1]),
                      str(ids[2]): dict(state="imported", olid=100 + ids[2])})            # imported earlier, book since deleted
    library.query(Item).delete(); library.commit()                                         # the admin deleted everything
    again = b.upsert_rows(bid, rows(4, title="Dune"))
    assert again["created"] == 2 and again["existing"] == 2                                # the two imported files are new rows
    got = b.list_rows(bid, limit=200)["rows"]
    by_state = {}
    for r in got:
        by_state.setdefault(r["state"], []).append(r)
    assert len(by_state["imported"]) == 2 and len(by_state["queued"]) == 3 and len(by_state["matched"]) == 1   # the duplicate is matched again
    assert all(r["in_library"] is False for r in got)


def test_older_imported_rows_release_their_key_once(library):
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(2))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(ids[0]): dict(state="imported", olid=5)})
    assert b.reconcile(bid)["released"] == 1 and b.reconcile(bid)["released"] == 0
    assert b.upsert_rows(bid, rows(2)) == {"created": 1, "existing": 1, "existing_states": {"queued": 1}}


def test_rows_say_whether_their_book_is_in_the_library_now(library):
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(3))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    put_in_library(library, 11, 12)
    set_state(bid, **{str(ids[0]): dict(state="imported", olid=11), str(ids[1]): dict(state="imported", olid=99)})
    got = {r["id"]: r["in_library"] for r in b.list_rows(bid)["rows"]}
    assert got == {ids[0]: True, ids[1]: False, ids[2]: False}
    assert b.reconcile(bid)["removed_from_library"] == 1                                   # the one whose book was deleted


def test_sync_over_http(client, library):
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1)]})
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    set_state(bid, **{str(rid): dict(state="duplicate", olid=555)})
    assert client.post(f"{API}/{bid}/sync").json() == {"reverted": 1, "released": 0, "removed_from_library": 0}
    assert client.get(f"{API}/{bid}/rows").json()["rows"][0]["state"] == "matched"
    assert client.post(f"{API}/{bid + 99}/sync").status_code == 404


# ─── every ISBN a file mentions ──────────────────────────────────────────────

def test_a_file_can_carry_several_isbns_and_all_are_kept_clean():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    b.upsert_rows(bid, [dict(path="a.epub", size=1, mtime=1, isbn="978-0-441-01359-3",
                             isbns=["0441013597", "9780441013593", "bogus", None, "978 0 14 032872 1", "9780140328721"])])
    row = b.list_rows(bid)["rows"][0]
    assert row["isbn"] == "9780441013593"
    assert row["isbn_alt"] == "0441013597 9780140328721"                  # de-duplicated, junk dropped, primary not repeated


def test_isbns_alone_are_enough_and_the_alternates_are_capped():
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    many = [f"97800000000{i:02d}" for i in range(10)]
    b.upsert_rows(bid, [dict(path="a.epub", size=1, mtime=1, isbns=many), dict(path="b.epub", size=2, mtime=1, isbns="x")])
    a, c = b.list_rows(bid)["rows"]
    assert a["isbn"] is not None and len(a["isbn_alt"].split()) <= b.MAX_ALT_ISBNS and len(a["isbn_alt"]) <= 64
    assert c["isbn"] is None and c["isbn_alt"] is None                    # not a list: ignored


def test_a_re_dropped_file_gains_isbns_it_did_not_have_before():
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    b.upsert_rows(bid, [dict(rows(1)[0], isbn="9780441013593", isbns=["9780140328721"])])
    row = b.list_rows(bid)["rows"][0]
    assert (row["isbn"], row["isbn_alt"]) == ("9780441013593", "9780140328721")


# ─── an imported row's stored book moves with its edition, even if a later step fails ───

def imported_row(olid=10):
    from lenny.core.bulk_import import batches as b
    bid = new_batch(); b.upsert_rows(bid, rows(1))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): dict(state="imported", olid=olid, match_title="Old", encrypted=False)})
    return bid, rid


def test_the_row_follows_the_stored_book_even_when_the_flag_update_fails():
    from lenny.core.api import LennyAPI
    from lenny.core.bulk_import import batches as b
    bid, rid = imported_row(10)
    with patch.object(LennyAPI, "rename_item") as rename, patch.object(LennyAPI, "update_item", side_effect=RuntimeError("db down")):
        with pytest.raises(RuntimeError):
            b.update_row(bid, rid, olid=11, encrypted=True, match={"title": "New", "author": "A", "year": 2020})
    row = b.list_rows(bid)["rows"][0]
    assert rename.call_count == 1
    assert (row["olid"], row["match_title"], row["match_year"], row["decision"]) == (11, "New", 2020, "user")   # the book moved: the row says so
    assert row["encrypted"] is False and row["state"] == "imported"                                              # the failed flag was not recorded


def test_a_row_removed_while_the_stored_book_is_moving_is_a_clean_not_found():
    from lenny.core.api import LennyAPI
    from lenny.core.bulk_import import batches as b
    from lenny.core.db import session as db
    bid, rid = imported_row(10)
    def rename_and_row_vanishes(old, new):
        db.query(b.ImportRow).filter(b.ImportRow.id == rid).delete(); db.commit()
    with patch.object(LennyAPI, "rename_item", rename_and_row_vanishes), patch.object(LennyAPI, "update_item"):
        with pytest.raises(b.ImportRowNotFound):
            b.update_row(bid, rid, olid=11)
    bid2, rid2 = imported_row(20)
    def flag_and_row_vanishes(*a, **k):
        db.query(b.ImportRow).filter(b.ImportRow.id == rid2).delete(); db.commit()
    with patch.object(LennyAPI, "update_item", flag_and_row_vanishes):
        with pytest.raises(b.ImportRowNotFound):
            b.update_row(bid2, rid2, encrypted=True)


def test_rename_and_flag_together_apply_both_when_all_goes_well():
    from lenny.core.api import LennyAPI
    from lenny.core.bulk_import import batches as b
    bid, rid = imported_row(10)
    with patch.object(LennyAPI, "rename_item") as rename, patch.object(LennyAPI, "update_item") as flag:
        out = b.update_row(bid, rid, olid=11, encrypted=True, match={"title": "New"})
    assert (out["olid"], out["encrypted"], out["match_title"]) == (11, True, "New")
    assert rename.call_args.args == (10, 11) and flag.call_args.args == (11,) and flag.call_args.kwargs == {"encrypted": True}
