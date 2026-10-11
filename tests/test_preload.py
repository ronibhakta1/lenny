"""Preloaded free books (#247): the catalog, picking, loading and the admin routes.

archive.org and Open Library are always faked.
"""

import threading
import time
from unittest.mock import patch

import pytest

pytest.importorskip("httpx")

SECRET = "internal-secret-for-tests"


def doc(n, title, author, source_id, subjects=("Fiction",), year=1900):
    return {"key": f"/works/OL{n}W", "title": title, "author_name": [author], "first_publish_year": year,
            "subject": list(subjects), "id_standard_ebooks": [source_id],
            "editions": {"docs": [{"key": f"/books/OL{n}M", "cover_i": n}]}}


DOCS = [
    doc(3, "Pride and Prejudice", "Jane Austen", "jane-austen/pride-and-prejudice", ("Fiction Classics", "Love stories")),
    doc(1, "Dracula", "Bram Stoker", "bram-stoker/dracula", ("Horror", "Gothic fiction")),
    doc(2, "The Raven", "Edgar Allan Poe", "edgar-allan-poe/the-raven", ("Poetry",)),
    doc(4, "Missing From Archive", "Nobody", "nobody/missing", ("Fiction",)),
]
FILES = {"jane-austen_pride-and-prejudice": 3000, "bram-stoker_dracula": 2000, "edgar-allan-poe_the-raven": 1000}


@pytest.fixture(autouse=True)
def fresh_catalog():
    from lenny.core.preload import catalog
    catalog._cached.update(built=0.0, books=[], known=0)
    catalog._refreshing.clear()
    yield
    catalog._cached.update(built=0.0, books=[], known=0)
    catalog._refreshing.clear()


@pytest.fixture
def fakes():
    """Fake archive.org file list and Open Library search; counts the builds."""
    from lenny.core.openlibrary import OpenLibraryRecord
    calls = {"archive": 0, "search": 0}

    def archive():
        calls["archive"] += 1
        return dict(FILES)

    def fetch():
        calls["search"] += 1
        return [OpenLibraryRecord(d) for d in DOCS]

    with patch("lenny.core.preload.catalog._archive_files", archive), \
         patch("lenny.core.preload.catalog._archive_item_names", lambda: set(calls.setdefault("items", set()))), \
         patch("lenny.core.preload.catalog._fetch_records", fetch):
        yield calls


@pytest.fixture
def db():
    from lenny.core.cache import CacheEntry
    from lenny.core.db import Base, engine, session
    from lenny.core.models import Item
    Base.metadata.create_all(engine)
    session.remove()
    yield session
    session.rollback()
    session.query(Item).delete()
    session.query(CacheEntry).delete()
    session.commit()
    session.remove()


def own(db, *olids):
    from lenny.core.models import FormatEnum, Item
    for o in olids:
        db.add(Item(openlibrary_edition=o, formats=FormatEnum.EPUB))
    db.commit()


# ─── catalog ─────────────────────────────────────────────────────────────────

def test_only_downloadable_books_are_in_the_catalog(fakes):
    from lenny.core.preload import catalog
    cat = catalog.get()
    assert [b["title"] for b in cat["books"]] == ["Dracula", "Pride and Prejudice", "The Raven"]   # sorted
    assert cat["known"] == 4                                  # Open Library knows one more
    dracula = cat["books"][0]
    assert dracula == {
        "olid": 1, "title": "Dracula", "authors": ["Bram Stoker"], "year": 1900,
        "cover_url": "https://covers.openlibrary.org/b/id/1-M.jpg", "subjects": ["Horror", "Gothic fiction"],
        "source": "standardebooks", "source_id": "bram-stoker/dracula", "size": 2000}


def test_a_record_without_editions_or_an_id_is_skipped_not_fatal(fakes):
    from lenny.core.openlibrary import OpenLibraryRecord
    from lenny.core.preload import catalog
    broken = [OpenLibraryRecord({"title": "No editions", "id_standard_ebooks": ["bram-stoker/dracula"], "editions": {"docs": []}}),
              OpenLibraryRecord({"title": "No id", "editions": {"docs": [{"key": "/books/OL9M"}]}}),
              OpenLibraryRecord(DOCS[1])]
    with patch("lenny.core.preload.catalog._fetch_records", return_value=broken):
        assert [b["olid"] for b in catalog.get()["books"]] == [1]


def test_the_catalog_is_built_once_then_reused(fakes):
    from lenny.core.preload import catalog
    now = [1000.0]
    catalog.get(now=lambda: now[0]); catalog.get(now=lambda: now[0] + 10)
    assert fakes["search"] == 1


def test_warm_builds_ahead_of_the_first_visitor_and_never_raises(fakes):
    import httpx
    from lenny.core.preload import catalog
    catalog.warm()
    assert fakes["search"] == 1 and len(catalog._cached["books"]) == 3
    catalog._cached.update(books=[], built=0.0)
    with patch("lenny.core.preload.catalog._archive_files", side_effect=httpx.ConnectError("down")):
        catalog.warm()                                                    # down at start-up: no crash, built on first use instead


def test_many_requests_at_once_build_it_once(fakes):
    from lenny.core.preload import catalog
    gate = threading.Barrier(8)
    def go():
        gate.wait(); catalog.get()
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert fakes["search"] == 1


def test_an_expired_catalog_is_served_at_once_while_a_new_one_builds_in_the_background(fakes):
    from lenny.core.preload import catalog
    catalog.get()
    catalog._cached["built"] = time.monotonic() - catalog.CATALOG_TTL - 1
    release, started = threading.Event(), threading.Event()
    real_fetch = catalog._fetch_records
    def slow_fetch():
        started.set(); release.wait(5)
        return real_fetch()
    with patch("lenny.core.preload.catalog._fetch_records", slow_fetch):
        began = time.monotonic()
        got = catalog.get()                                               # not blocked by the rebuild
        assert time.monotonic() - began < 1 and len(got["books"]) == 3
        assert started.wait(2) and catalog._refreshing.is_set()
        catalog.get()                                                     # a second call does not start a second rebuild
        release.set()
        for _ in range(50):
            if not catalog._refreshing.is_set():
                break
            time.sleep(0.05)
    assert fakes["search"] == 2 and catalog._cached["built"] > time.monotonic() - 5


def test_a_failed_rebuild_keeps_serving_the_old_catalog(fakes):
    import httpx
    from lenny.core.preload import catalog
    catalog.get()
    catalog._cached["built"] = time.monotonic() - catalog.CATALOG_TTL - 1
    with patch("lenny.core.preload.catalog._archive_files", side_effect=httpx.ConnectError("down")):
        assert len(catalog.get()["books"]) == 3
        for _ in range(50):
            if not catalog._refreshing.is_set():
                break
            time.sleep(0.05)
        assert len(catalog.get()["books"]) == 3 and not catalog._refreshing.is_set()


def test_no_catalog_and_no_upstream_is_a_clear_error():
    import httpx
    from lenny.core.preload import catalog
    with patch("lenny.core.preload.catalog._archive_files", side_effect=httpx.ConnectError("down")):
        with pytest.raises(catalog.CatalogUnavailable):
            catalog.get()


def test_all_pages_are_fetched_and_the_extra_pages_in_parallel():
    from lenny.core.preload import catalog
    calls, lock, live, peak = [], threading.Lock(), [0], [0]
    def page(query, fields=None, page=1, limit=100):
        with lock:
            calls.append(page); live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return {"numFound": 450, "docs": [DOCS[0]] if page == 1 else [DOCS[page % 3]]}
    with patch("lenny.core.preload.catalog.OpenLibrary.search_json", page):
        records = catalog._fetch_records()
    assert sorted(calls) == [1, 2, 3, 4, 5] and len(records) == 5          # 450 results = 5 pages of 100
    assert 1 < peak[0] <= catalog.FETCH_WORKERS                            # pages 2..5 ran together, bounded
    with patch("lenny.core.preload.catalog.OpenLibrary.search_json", lambda *a, **k: {"numFound": 1, "docs": [DOCS[0]]}):
        assert len(catalog._fetch_records()) == 1                           # one page: no pool needed
    with patch("lenny.core.preload.catalog.OpenLibrary.search_json", lambda *a, **k: "garbage"):
        assert catalog._fetch_records() == []


# ─── search, collections, picking ────────────────────────────────────────────

def test_search_matches_title_author_and_subject_words(fakes):
    from lenny.core.preload import catalog
    titles = lambda **kw: [b["title"] for b in catalog.search(**kw)["books"]]
    assert titles(q="drac") == ["Dracula"]
    assert titles(q="jane austen") == ["Pride and Prejudice"]
    assert titles(q="POETRY") == ["The Raven"]
    assert titles(q="austen dracula") == []                    # every word must match
    assert titles(q="") == ["Dracula", "Pride and Prejudice", "The Raven"]


def test_search_pages_and_reports_totals(fakes):
    from lenny.core.preload import catalog
    first = catalog.search(limit=2)
    assert len(first["books"]) == 2 and first["next_cursor"] == 2
    assert (first["total"], first["available"], first["known"]) == (3, 3, 4)
    last = catalog.search(cursor=2, limit=2)
    assert len(last["books"]) == 1 and last["next_cursor"] is None
    assert catalog.search(cursor=-5, limit=0)["books"]          # clamped, not an error


def test_search_can_hide_owned_books_and_filter_by_collection(fakes):
    from lenny.core.preload import catalog
    assert [b["olid"] for b in catalog.search(hide={1})["books"]] == [3, 2]
    assert [b["title"] for b in catalog.search(collection="poetry")["books"]] == ["The Raven"]
    with pytest.raises(ValueError):
        catalog.search(collection="nope")


def test_collections_count_members_and_owned_and_hide_empty_ones(fakes):
    from lenny.core.preload import catalog
    got = {c["id"]: c for c in catalog.collections(existing={2})}
    assert got["poetry"] == {"id": "poetry", "name": "Poetry", "count": 1, "in_library": 1}
    assert got["horror"]["count"] == 1 and got["romance"]["count"] == 1
    assert "science-fiction" not in got


def test_find_resolves_ids_source_ids_and_collections(fakes):
    from lenny.core.preload import catalog
    books, unknown = catalog.find(olids=[1, 99], source_ids=["edgar-allan-poe/the-raven", "x/y"], collection="romance")
    assert sorted(b["olid"] for b in books) == [1, 2, 3]
    assert unknown == [99, "x/y"]
    assert catalog.find(olids=[1, 1])[0] == catalog.find(olids=[1])[0]       # no duplicates


# ─── loading ─────────────────────────────────────────────────────────────────

def test_plan_leaves_out_books_already_in_the_library(fakes, db):
    from lenny.core.preload import service
    own(db, 1)
    plan = service.plan(olids=[1, 2, 99])
    assert [b["olid"] for b in plan["to_load"]] == [2]
    assert plan["already_in_library"] == 1 and plan["unknown"] == [99]


def test_plan_refuses_a_huge_pick(fakes):
    from lenny.core.preload import service
    with pytest.raises(ValueError):
        service.plan(olids=list(range(service.MAX_PICK + 1)))


def test_load_queues_everything_then_continues_past_a_failure(fakes, db):
    from lenny.core.imports import ImportJob
    from lenny.core.preload import catalog, service
    books = catalog.get()["books"]
    seen = []
    def fake_import(olid, source_id, title=None):
        seen.append((olid, [r["status"] for r in ImportJob.list() if r["olid"] == olid]))
        return {1: "failed", 2: "uploaded", 3: "not_in_set"}[olid]
    with patch("lenny.core.preload.service.import_one", fake_import):
        counts = service.load(books)
    assert counts == {"uploaded": 1, "failed": 1, "not_in_set": 1}
    assert all(statuses == ["pending"] for _, statuses in seen[:1])     # queued before any started
    assert {r["title"] for r in ImportJob.list()} >= {"Dracula"}


def test_a_second_load_while_one_runs_does_nothing(fakes, db):
    from lenny.core.preload import catalog, service
    books = catalog.get()["books"]
    started, release, results = threading.Event(), threading.Event(), []
    def slow(*a, **k):
        started.set(); release.wait(5); return "uploaded"
    with patch("lenny.core.preload.service.import_one", slow):
        t = threading.Thread(target=lambda: results.append(service.load(books[:1])))
        t.start(); started.wait(5)
        assert service.load(books) == {"uploaded": 0, "failed": 0, "not_in_set": 0}
        release.set(); t.join()
    assert results[0]["uploaded"] == 1


def test_running_reflects_live_progress_and_forgets_stale_runs(db):
    from lenny.core.cache import CacheEntry
    from lenny.core.imports import DONE, DOWNLOADING, ImportJob
    from lenny.core.preload import service
    from datetime import datetime, timedelta, timezone
    assert service.running() is False
    ImportJob.record("standardebooks", 1, DOWNLOADING)
    assert service.running() is True
    ImportJob.record("briet", 5, DOWNLOADING)                      # another source does not count
    ImportJob.record("standardebooks", 1, DONE)
    assert service.running() is False
    ImportJob.record("standardebooks", 2, DOWNLOADING)
    db.query(CacheEntry).filter(CacheEntry.key == "standardebooks:2").update(
        {"created_at": datetime.now(timezone.utc) - timedelta(hours=2)}); db.commit()
    assert service.running() is False                              # a crashed run stops blocking


def test_import_one_outcomes(db):
    from lenny.core import standardebooks as se
    import io
    ok = io.BytesIO(b"PK\x03\x04" + b"0" * 50)
    with patch.object(se.StandardEbooks, "download", return_value=None):
        assert se.import_one(1, "a/b") == se.NOT_IN_SET
    with patch.object(se.StandardEbooks, "download", return_value=io.BytesIO(b"not a zip")):
        assert se.import_one(1, "a/b") == se.FAILED_
    with patch.object(se.StandardEbooks, "download", return_value=ok), patch.object(se.LennyClient, "upload", return_value=False):
        assert se.import_one(1, "a/b") == se.FAILED_
    with patch.object(se.StandardEbooks, "download", side_effect=RuntimeError("boom")):
        assert se.import_one(1, "a/b") == se.FAILED_                # never raises
    with patch.object(se.StandardEbooks, "download", return_value=io.BytesIO(ok.getvalue())), \
         patch.object(se.LennyClient, "upload", return_value=True):
        assert se.import_one(1, "a/b", title="T") == se.UPLOADED


# ─── routes ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app_module():
    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield app


@pytest.fixture
def client(app_module):
    from fastapi.testclient import TestClient
    from lenny.core import auth
    with patch.object(auth, "ADMIN_INTERNAL_SECRET", SECRET), \
         patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None):
        headers = {"X-Admin-Internal-Secret": SECRET, "Authorization": f"Bearer {auth.issue_admin_token()}"}
        c = TestClient(app_module, headers=headers)
        yield c
        c.close()


P = "/v1/api/admin/preload"


@pytest.mark.parametrize("method, path", [("get", "/catalog"), ("get", "/collections"), ("post", "/import")])
def test_every_route_requires_the_admin_credentials(app_module, method, path):
    from fastapi.testclient import TestClient
    assert TestClient(app_module).request(method.upper(), P + path, json={}).status_code == 403


def test_catalog_route_marks_owned_books_and_filters(client, fakes, db):
    own(db, 2)
    body = client.get(f"{P}/catalog", params={"limit": 10}).json()
    assert [(b["title"], b["in_library"]) for b in body["books"]] == [
        ("Dracula", False), ("Pride and Prejudice", False), ("The Raven", True)]
    assert (body["available"], body["known"], body["in_library"]) == (3, 4, 1)
    hidden = client.get(f"{P}/catalog", params={"hide_owned": True}).json()
    assert [b["title"] for b in hidden["books"]] == ["Dracula", "Pride and Prejudice"]
    assert client.get(f"{P}/catalog", params={"q": "poe"}).json()["total"] == 1
    assert client.get(f"{P}/catalog", params={"collection": "bogus"}).status_code == 400


def test_collections_route(client, fakes, db):
    own(db, 2)
    got = {c["id"]: c for c in client.get(f"{P}/collections").json()["collections"]}
    assert got["poetry"]["in_library"] == 1 and got["horror"]["in_library"] == 0


def test_routes_say_502_when_nothing_can_be_built(client):
    import httpx
    with patch("lenny.core.preload.catalog._archive_files", side_effect=httpx.ConnectError("down")):
        assert client.get(f"{P}/catalog").status_code == 502
        assert client.post(f"{P}/import", json={"olids": [1]}).status_code == 502


def test_import_route_queues_loads_and_reports(client, fakes, db):
    own(db, 1)
    loaded = []
    with patch("lenny.core.preload.service.import_one",
               lambda olid, sid, title=None: loaded.append((olid, sid, title)) or "uploaded"):
        r = client.post(f"{P}/import", json={"olids": [1, 2, 99], "source_ids": ["jane-austen/pride-and-prejudice"]})
    assert r.status_code == 202
    assert r.json() == {"queued": 2, "already_in_library": 1, "unknown": [99], "size": 4000}
    assert sorted(loaded) == [(2, "edgar-allan-poe/the-raven", "The Raven"),
                              (3, "jane-austen/pride-and-prejudice", "Pride and Prejudice")]


def test_import_route_by_collection(client, fakes, db):
    loaded = []
    with patch("lenny.core.preload.service.import_one", lambda olid, *a, **k: loaded.append(olid) or "uploaded"):
        assert client.post(f"{P}/import", json={"collection": "poetry"}).json()["queued"] == 1
    assert loaded == [2]


def test_import_route_refuses_while_a_load_is_running(client, fakes, db):
    from lenny.core.imports import DOWNLOADING, ImportJob
    ImportJob.record("standardebooks", 1, DOWNLOADING)
    with patch("lenny.core.preload.service.import_one") as imp:
        assert client.post(f"{P}/import", json={"olids": [2]}).status_code == 409
    assert imp.call_count == 0


@pytest.mark.parametrize("body", [{}, {"olids": []}, {"olids": "1"}, {"olids": [True]}, {"olids": [1.5]},
                                  {"source_ids": [1]}, {"collection": 5}, {"collection": "nope"},
                                  {"olids": list(range(1, 202))}])
def test_import_route_rejects_bad_picks(client, fakes, db, body):
    with patch("lenny.core.preload.service.import_one") as imp:
        assert client.post(f"{P}/import", json=body).status_code == 400
    assert imp.call_count == 0


def test_nothing_to_load_starts_nothing(client, fakes, db):
    own(db, 1)
    with patch("lenny.core.preload.service.import_one") as imp:
        r = client.post(f"{P}/import", json={"olids": [1]})
    assert r.status_code == 202 and r.json()["queued"] == 0 and imp.call_count == 0


# ─── more of Standard Ebooks than the preload item: standalone archive.org items ──

def test_standalone_archive_items_are_added_to_the_catalog_with_unknown_size(fakes):
    from lenny.core.preload import catalog
    fakes["items"] = {"edgar-allan-poe_the-raven", "nobody_missing"}          # the raven also in the preload item; "missing" is extra
    cat = catalog.get()
    assert len(cat["books"]) == 4 and cat["known"] == 4                       # "Missing From Archive" is now downloadable
    sizes = {b["title"]: b["size"] for b in cat["books"]}
    assert sizes["The Raven"] == 1000                                         # the preload item's known size wins
    assert sizes["Missing From Archive"] == 0                                 # unknown size from a standalone item


def test_a_failed_standalone_search_falls_back_to_the_preload_item_alone(fakes):
    import httpx
    from lenny.core.preload import catalog
    with patch("lenny.core.preload.catalog._archive_item_names", side_effect=httpx.ConnectError("down")):
        assert len(catalog.get()["books"]) == 3


def test_standalone_items_are_listed_with_one_search():
    from lenny.core.preload import catalog
    body = {"response": {"docs": [{"identifier": "a_b", "format": ["EPUB", "Text PDF"]},
                                  {"identifier": "c_d", "format": ["Text PDF"]},          # no EPUB: not loadable
                                  {"identifier": "e_f"}]}}
    resp = type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: body})()
    with patch("lenny.core.preload.catalog.httpx.get", return_value=resp) as get:
        assert catalog._archive_item_names() == {"a_b"}
    assert get.call_count == 1 and "Standard eBooks" in get.call_args.kwargs["params"]["q"]


def test_a_book_missing_from_the_preload_item_is_downloaded_from_its_own_item():
    import io
    from lenny.core.standardebooks import StandardEbooks
    epub = io.BytesIO(b"PK\x03\x04" + b"0" * 50)
    urls = []
    def fake_download(url, timeout=None, headers=None):
        urls.append(url)
        return epub if "/jane-austen_emma/" in url else None
    with patch("lenny.core.standardebooks.download_epub", fake_download):
        assert StandardEbooks.download("jane-austen/emma") is epub
        assert StandardEbooks.download("nobody/nothing") is None
    assert urls[0].endswith("lenny-open-access-preloads/jane-austen_emma.epub")        # the preload item is tried first
    assert urls[1] == "https://archive.org/download/jane-austen_emma/jane-austen_emma.epub"
    assert urls[2:] == [StandardEbooks.construct_download_url("nobody/nothing"), StandardEbooks.construct_item_url("nobody/nothing")]


# ─── junk in Open Library's id_standard_ebooks field ─────────────────────────

@pytest.mark.parametrize("raw, clean", [
    ("jane-austen/emma", "jane-austen/emma"),
    ("/sophocles/antigone/francis-storr", "sophocles/antigone/francis-storr"),         # leading slash
    ("fyodor-sologub/the-created-legend/john-cournos/", "fyodor-sologub/the-created-legend/john-cournos"),
    ("  Jane-Austen/Emma ", "jane-austen/emma"),
    ("3c85ef23-cc51-4320-96e1-f80f189ce31e", None),                                      # a UUID: not Standard Ebooks
    ("/3c85ef23-cc51-4320-96e1-f80f189ce31e", None),
    ("emma", None), ("", None), (None, None), ("a/b/c/d/e", None), ("a b/c", None), ("../etc/passwd", None),
])
def test_clean_source_id(raw, clean):
    from lenny.core.standardebooks import clean_source_id
    assert clean_source_id(raw) == clean


def test_the_catalog_skips_junk_ids_and_counts_only_real_ones(fakes):
    from lenny.core.openlibrary import OpenLibraryRecord
    from lenny.core.preload import catalog
    junk = [OpenLibraryRecord(dict(DOCS[0], **{"id_standard_ebooks": ["3c85ef23-cc51-4320-96e1-f80f189ce31e"]})),
            OpenLibraryRecord(dict(DOCS[2], **{"id_standard_ebooks": ["/edgar-allan-poe/the-raven"]}))]   # fixable
    with patch("lenny.core.preload.catalog._fetch_records", return_value=junk + [OpenLibraryRecord(DOCS[1])]):
        cat = catalog.get()
    assert [b["source_id"] for b in cat["books"]] == ["bram-stoker/dracula", "edgar-allan-poe/the-raven"]
    assert cat["known"] == 2                                                                           # the UUID entry is not counted
