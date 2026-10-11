"""Matching and importing a batch (#246): scoring, Open Library calls, the
background job, the import step and their routes.

Open Library is always faked. Concurrency that needs row locks runs on Postgres
when LENNY_TEST_PG_URL is set (see test_batches.py).
"""

import io
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tests.bulk_import.test_batches import (  # noqa: F401  (fixtures and helpers shared)
    API, admin_headers, app_module, clean_tables, client, new_batch, pg, rows, set_state)

os.environ.setdefault("TESTING", "true")

IMPORT = "/v1/api/admin/import"


@pytest.fixture(autouse=True)
def no_leftover_jobs():
    from lenny.core.bulk_import import jobs
    jobs._jobs.clear()
    yield
    jobs._jobs.clear()


# ─── a fake Open Library ─────────────────────────────────────────────────────

def work(title, authors, *editions, year=2000):
    """editions: (olid, [isbns], language)"""
    return {"key": "/works/OL1W", "title": title, "author_name": authors, "first_publish_year": year,
            "editions": {"docs": [
                {"key": f"/books/OL{o}M", "title": title, "isbn": isbns, "language": [lang],
                 "publisher": ["Pub"], "publish_year": [year]} for o, isbns, lang in editions]}}


class FakeOL:
    """Stands in for matcher.search: answers isbn:(…) and title/author queries from a catalogue."""

    def __init__(self, *works):
        self.works, self.calls = list(works), []

    def __call__(self, params, retries=3, fresh=False):
        self.calls.append(dict(params))
        if "q" in params and params["q"].startswith("isbn:"):
            wanted = set(params["q"].replace("isbn:", "").strip("()").replace(" OR ", " ").split())
            return [w for w in self.works if any(set(e["isbn"]) & wanted for e in w["editions"]["docs"])]
        if "publisher:" in params.get("q", "") or "publish_year:" in params.get("q", ""):
            return []    # this fake has no per-edition data to filter on: like Open Library with no such edition
        if "q" in params and 'title:"' not in params["q"]:   # free words, like the search box
            words = params["q"].lower().split()
            return [w for w in self.works if all(x in (w["title"] + " " + " ".join(w["author_name"])).lower() for x in words)]
        if "q" in params:   # a batched query: (title:"X" AND author:"Y") OR (...)
            import re
            asks = [(t, a) for t, a in re.findall(r'title:"([^"]*)"(?: AND author:"([^"]*)")?', params["q"])]
        else:
            asks = [(params.get("title", ""), params.get("author", ""))]
        out = []
        for t, a in asks:
            t, a = t.lower(), a.lower()
            found = [w for w in self.works if t and t.split(":")[0] in w["title"].lower()]
            # Like Open Library: an author term that matches no author empties the result.
            out += [w for w in found if (not a or any(a in n.lower() for n in w["author_name"])) and w not in out]
        return out


@pytest.fixture
def ol():
    fake = FakeOL(
        work("Dune", ["Frank Herbert"], (101, ["9780441013593", "0441013597"], "eng"), (102, ["9780593099322"], "fre")),
        work("The Left Hand of Darkness", ["Ursula K. Le Guin"], (201, ["9780441478125"], "eng")),
    )
    with patch("lenny.core.bulk_import.matcher.search", fake), \
         patch("lenny.core.bulk_import.matcher.work_editions", return_value=[]):
        yield fake


# ─── scoring and decisions ───────────────────────────────────────────────────

def test_candidates_are_flattened_deduplicated_and_skip_odd_keys():
    from lenny.core.bulk_import import matcher as m
    docs = [work("A", ["X"], (1, ["111"], "eng"), (1, ["111"], "eng"), (2, [], "eng"))]
    docs[0]["editions"]["docs"].append({"key": "/works/OL9W", "title": "not an edition"})
    got = m.candidates_from(docs)
    assert [c["olid"] for c in got] == [1, 2]
    assert got[0]["authors"] == ["X"] and got[0]["isbn"] == "111"


@pytest.mark.parametrize("row, expected", [
    (dict(isbn="9780441013593", title="whatever"), 100),
    (dict(title="Dune", author="Frank Herbert"), 100),
    (dict(title="Dune", author="Herbert, Frank"), 100),             # Last, First
    (dict(title="Dune: Messiah", author="Frank Herbert"), 86),      # a different book: review, never auto-picked
    (dict(title="DUNE", author="frank   herbert"), 100),
    (dict(title="Dünë", author="Frank Herbert"), 100),              # accents
    (dict(title="Dune", author="Frank Herbert; Brian Herbert"), 100),
    (dict(title="Dune"), 90),                                       # title alone is capped
    (dict(title="Cooking for Cats", author="Nobody"), None),        # unrelated: checked below
    (dict(author="Frank Herbert"), 0),                              # no title
])
def test_scores(row, expected):
    from lenny.core.bulk_import import matcher as m
    cand = m.candidates_from([work("Dune", ["Frank Herbert"], (101, ["9780441013593"], "eng"))])[0]
    s = m.score(row, cand)
    if expected is None:
        assert s < m.REVIEW_MIN
    else:
        assert s == expected
    if expected == 86:
        assert m.REVIEW_MIN <= s < m.AUTO_MIN


def test_decide_thresholds():
    from lenny.core.bulk_import import matcher as m
    cand = lambda s: [{"olid": 7, "title": "T", "authors": ["A"], "score": s}]
    assert m.decide(cand(m.AUTO_MIN))["state"] == "matched"
    assert m.decide(cand(m.AUTO_MIN))["olid"] == 7 and m.decide(cand(m.AUTO_MIN))["auto_picked"] is True
    review = m.decide(cand(m.AUTO_MIN - 1))
    assert review["state"] == "needs_review" and review["olid"] is None and review["auto_picked"] is False
    assert m.decide(cand(m.REVIEW_MIN - 1))["state"] == "no_match"
    empty = m.decide([])
    assert empty["state"] == "no_match" and empty["candidates_count"] == 0


def test_ties_prefer_the_files_language_then_an_isbn():
    from lenny.core.bulk_import import matcher as m
    cands = m.candidates_from([work("Dune", ["Frank Herbert"], (5, [], "eng"), (6, ["1"], "fre"), (7, ["2"], "eng"))])
    order = [c["olid"] for c in m.rank(dict(title="Dune", author="Frank Herbert", language="eng"), cands)]
    assert order == [7, 5, 6]


# ─── forgiving matches (the Defector Weekly Digest case) ─────────────────────

MASTHEAD = ("Defector, Diana Moskovitz, Luis Paez-Pumar, Lauren Theisen, Patrick Redford, Matt Ribel, "
            "David Roth, Dave McKenna, Abigail Segel")


@pytest.fixture
def defector():
    fake = FakeOL(
        work("Defector Weekly Digest: May 17-23, 2026", ["Tom Ley", "Diana Moskovitz", "Luis Paez-Pumar", "Lauren Theisen"],
             (62576958, [], "eng"), year=2026),
        work("The Suitors", ["Ben Ehrenreich"], (3413631, [], "eng")),
        work("Database Internals", ["Alex Petrov"], (28933309, [], "eng")),
    )
    with patch("lenny.core.bulk_import.matcher.search", fake), \
         patch("lenny.core.bulk_import.matcher.work_editions", return_value=[]):
        yield fake


def test_a_long_author_list_does_not_empty_the_search(defector):
    row = dict(id=1, isbn=None, title="Defector Weekly Digest", author=MASTHEAD, language=None,
               path="ebooks/defector-weekly-digest_may_8-25-26.epub")
    out = match([row])[1]
    assert out["state"] == "matched" and out["olid"] == 62576958 and out["confidence"] >= 90
    assert "author" not in defector.calls[0]            # a masthead is not a search term


def test_a_publisher_as_author_widens_to_a_title_only_search(defector):
    row = dict(id=1, isbn=None, title="Defector Weekly Digest", author="Defector", language=None, path=None)
    out = match([row])[1]
    assert [("author" in c) for c in defector.calls] == [True, False]      # tried with, then without
    assert out["state"] in ("matched", "needs_review") and out["candidates_count"] == 1


def test_a_clean_file_costs_one_request(defector):
    out = match([dict(id=1, isbn=None, title="The Suitors", author="Ben Ehrenreich", language=None, path=None)])
    assert out[1]["state"] == "matched" and len(defector.calls) == 1


def test_file_name_is_used_when_metadata_is_missing(defector):
    for path, olid in (("ebooks/ben-ehrenreich_the-suitors_9-17-26.epub", 3413631),
                       ("ebooks/Database Internals (Alex Petrov) (Z-Library).epub", 28933309)):
        out = match([dict(id=1, isbn=None, title=None, author=None, language=None, path=path)])[1]
        assert out["state"] == "matched" and out["olid"] == olid, path


def test_a_title_guessed_from_the_file_name_alone_is_never_auto_picked():
    fake = FakeOL(work("1984", ["George Orwell"], (1891131, [], "eng")))
    with patch("lenny.core.bulk_import.matcher.search", fake):
        out = match([dict(id=1, isbn=None, title=None, author=None, language=None, path="ebooks/orwell_1984.epub")])[1]
    assert out["state"] == "needs_review" and out["olid"] is None


def test_a_row_with_neither_metadata_nor_a_usable_name_is_no_match(defector):
    out = match([dict(id=1, isbn=None, title=None, author=None, language=None, path="x/_.epub")])[1]
    assert out["state"] == "no_match" and defector.calls == []


def test_the_file_having_a_subtitle_the_record_lacks_is_never_auto_picked(defector):
    fake = FakeOL(work("Dune", ["Frank Herbert"], (101, [], "eng")))
    with patch("lenny.core.bulk_import.matcher.search", fake):
        out = match([dict(id=1, isbn=None, title="Dune: Messiah", author="Frank Herbert", language=None, path=None)])[1]
    assert out["state"] == "needs_review"


def test_candidates_for_tries_every_query_for_the_widest_list(defector):
    from lenny.core.bulk_import import matcher as m
    row = dict(isbn=None, title="Defector Weekly Digest", author="Defector", language=None,
               path="ebooks/defector-weekly-digest_may_8.epub")
    got = m.candidates_for(row)
    assert [c["olid"] for c in got] == [62576958] and len(defector.calls) >= 3


@pytest.mark.parametrize("author, names", [
    ("Frank Herbert; Brian Herbert", ["Frank Herbert", "Brian Herbert"]),
    ("Herbert, Frank", ["Frank Herbert"]),
    ("Le Guin, Ursula K.", ["Ursula K. Le Guin"]),
    ("van Gogh, Vincent", ["Vincent van Gogh"]),
    ("Andrew Hunt, David Thomas", ["Andrew Hunt", "David Thomas"]),
    ("A One & B Two and C Three", ["A One", "B Two", "C Three"]),
    (MASTHEAD, ["Defector", "Diana Moskovitz", "Luis Paez-Pumar", "Lauren Theisen", "Patrick Redford",
                "Matt Ribel", "David Roth", "Dave McKenna", "Abigail Segel"]),
    (None, []), ("  ", []),
])
def test_author_parsing(author, names):
    from lenny.core.bulk_import import matcher as m
    assert m._split_names(author) == names


def test_names_compare_ignoring_word_order():
    from lenny.core.bulk_import import matcher as m
    assert m._name_ratio("Moskovitz, Diana", "Diana Moskovitz") == 1.0
    assert m._name_ratio("Diana Moskovitz", "Frank Herbert") < 0.5


@pytest.mark.parametrize("path, expected", [
    ("a/b/ben-ehrenreich_the-suitors_9-17-26.epub", {"author": "ben ehrenreich", "title": "the suitors"}),
    ("Database Internals (Alex Petrov) (Z-Library).epub", {"title": "Database Internals", "author": "Alex Petrov"}),
    ("A Plain Title.EPUB", {"title": "A Plain Title"}),
    ("defector-weekly-digest_may_8-25-26.epub", {"title": "defector-weekly-digest"}),
    ("orwell_1984.epub", {"author": "orwell", "title": "1984"}),
    ("", {"title": ""}),
])
def test_from_path(path, expected):
    from lenny.core.bulk_import import matcher as m
    assert m.from_path(path) == expected


# ─── speed: dedupe, cache, bulk save ─────────────────────────────────────────

@pytest.fixture
def clear_cache():
    from lenny.core.bulk_import import matcher as m
    m._cache.clear()
    yield
    m._cache.clear()


def test_identical_rows_are_looked_up_once_and_share_the_answer(ol):
    from lenny.core.bulk_import import matcher as m
    rows = [titled(i, "Dune", "Frank Herbert") for i in range(5)] + [titled(9, "The Left Hand of Darkness", "Ursula K. Le Guin")]
    out = dict(m.match_rows(rows))
    assert len(out) == 6 and {out[i]["olid"] for i in range(5)} == {101} and out[9]["olid"] == 201
    assert len(ol.calls) <= 2                               # not 6
    out[0]["state"] = "changed"
    assert out[1]["state"] == "matched"                      # each row got its own copy


def test_rows_that_differ_in_language_are_not_merged(ol):
    from lenny.core.bulk_import import matcher as m
    en, fr = titled(1, "Dune", "Frank Herbert"), titled(2, "Dune", "Frank Herbert")
    en["language"], fr["language"] = "en", "fr"
    assert m._same_book(en) != m._same_book(fr)
    assert len(dict(m.match_rows([en, fr]))) == 2


def test_search_answers_are_cached_but_empty_ones_and_fresh_calls_are_not(clear_cache, fresh_guard):
    from lenny.core.bulk_import import matcher as m
    body = {"docs": [{"key": "/works/OL1W"}]}
    with patch("requests.get", side_effect=[resp(200, body), resp(200, {"docs": []}), resp(200, {"docs": []}), resp(200, body)]) as get:
        assert m.search({"title": "a"}) and m.search({"title": "a"})          # second is a hit
        assert get.call_count == 1
        assert m.search({"title": "b"}) == [] and m.search({"title": "b"}) == []   # empty: asked again
        assert get.call_count == 3
        m.search({"title": "a"}, fresh=True)                                    # Re-check: skips the cache
        assert get.call_count == 4


def test_removing_a_row_forgets_its_cached_lookups(clear_cache, fresh_guard):
    from lenny.core.bulk_import import matcher as m
    body = {"docs": [{"key": "/works/OL1W"}]}
    row = dict(isbn="9780441013593", title="Dune", author="Frank Herbert", path=None)
    with patch("requests.get", return_value=resp(200, body)) as get:
        m.search({"title": "Dune", "author": "Frank Herbert", "limit": m.MAX_CANDIDATES})
        m.search({"title": "Dune", "limit": m.MAX_CANDIDATES})
        m.search({"q": "isbn:9780441013593", "limit": m.MAX_CANDIDATES})
        m.search({"title": "Other Book", "limit": m.MAX_CANDIDATES})
        assert get.call_count == 4 and len(m._cache) == 4
        m.forget(row)
        assert len(m._cache) == 1                                      # only the unrelated lookup is left
        m.clear_cache()
        assert len(m._cache) == 0


def test_removing_a_row_over_http_purges_its_cache_and_a_redrop_is_fresh(client, ol):
    from lenny.core.bulk_import import jobs, matcher as m
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1, title="Dune", author="Frank Herbert")]})
    with patch.object(jobs, "_executor", Inline()):
        client.post(f"{API}/{bid}/match")
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    assert client.get(f"{API}/{bid}/rows").json()["rows"][0]["olid"] == 101
    with patch.object(m, "forget") as forget:
        r = client.delete(f"{API}/{bid}/rows/{rid}")
    assert r.status_code == 200 and r.json()["total"] == 0 and forget.call_count == 1
    assert forget.call_args.args[0]["title"] == "Dune"
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1, title="Dune", author="Frank Herbert")]})
    again = client.get(f"{API}/{bid}/rows").json()["rows"][0]
    assert again["state"] == "queued" and again["olid"] is None and again["decision"] == "auto"


def test_the_cache_is_bounded_and_expires(clear_cache, fresh_guard):
    from lenny.core.bulk_import import matcher as m
    body = {"docs": [{"key": "/works/OL1W"}]}
    with patch("requests.get", return_value=resp(200, body)) as get, patch.object(m, "CACHE_MAX", 3):
        for i in range(6):
            m.search({"title": str(i)})
        assert len(m._cache) == 3
        before = get.call_count
        m.search({"title": "5"}); assert get.call_count == before          # newest kept
        m.search({"title": "0"}); assert get.call_count == before + 1      # oldest evicted
        with patch.object(m, "CACHE_TTL", -1):
            m.search({"title": "5"}); assert get.call_count == before + 2  # expired


def test_a_recheck_job_and_the_candidates_list_bypass_the_cache(clear_cache, fresh_guard):
    from lenny.core.bulk_import import matcher as m
    seen = []
    def spy(params, retries=3, fresh=False):
        seen.append(fresh); return []
    with patch("lenny.core.bulk_import.matcher.search", spy):
        list(m.match_rows([titled(1, "x")], fresh=True))
        list(m.match_rows([titled(2, "y")]))
        m.candidates_for(dict(isbn=None, title="z", author=None, language=None, path=None))
    assert seen[0] is True and seen[-1] is True and seen[1:2] == [False] * len(seen[1:2])


def test_bulk_save_writes_many_rows_and_skips_decided_ones(ol):
    from lenny.core.bulk_import import batches as b, jobs
    bid = batch_with(*[dict(title=f"T{i}") for i in range(6)])
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    b.update_row(bid, ids[0], olid=999)                                         # decided: untouched
    results = [(i, dict(state="matched", olid=1000 + i, auto_picked=True, candidates_count=1, confidence=95,
                        match_title="M", match_author="A", error=None)) for i in ids[:5]]
    results.append((ids[5], dict(state="no_match", candidates_count=0, confidence=10, error="nope")))
    jobs._save(bid, results, "queued")
    got = {r["id"]: r for r in b.list_rows(bid)["rows"]}
    assert got[ids[0]]["olid"] == 999 and got[ids[0]]["decision"] == "user"
    assert [got[i]["olid"] for i in ids[1:5]] == [1000 + i for i in ids[1:5]] and got[ids[1]]["match_title"] == "M"
    assert got[ids[5]]["state"] == "no_match" and got[ids[5]]["olid"] is None and got[ids[5]]["error"] == "nope"


def test_bulk_save_of_many_rows_is_a_handful_of_statements(ol):
    from sqlalchemy import event
    from lenny.core.bulk_import import batches as b, jobs
    from lenny.core.db import engine
    bid = new_batch(); b.upsert_rows(bid, rows(300, title="x"))
    ids = [r["id"] for r in b.list_rows(bid, limit=200)["rows"]] + [r["id"] for r in b.list_rows(bid, cursor=200, limit=200)["rows"]]
    statements = []
    results = [(i, dict(state="matched", olid=i, confidence=90)) for i in ids]
    def record(conn, cursor, statement, *rest):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        jobs._save(bid, results, "queued")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    updates = [x for x in statements if x.startswith("UPDATE import_rows")]
    assert len(updates) <= 2                                                      # one executemany, not 300 UPDATEs


# ─── speed: several rows per request, a few requests at once ─────────────────

def titled(i, title, author=None, **extra):
    return dict(id=i, isbn=None, title=title, author=author, language=None, path=None, **extra)


def test_title_rows_go_ten_to_a_request_and_only_misses_are_searched_again():
    from lenny.core.bulk_import import matcher as m
    books = [work(f"Book Number {i}", [f"Writer {i}"], (1000 + i, [], "eng")) for i in range(25)]
    fake = FakeOL(*books)
    rows = [titled(i, f"Book Number {i}", f"Writer {i}") for i in range(25)] + [titled(99, "Not In Open Library", "Nobody")]
    with patch("lenny.core.bulk_import.matcher.search", fake):
        out = dict(m.match_rows(rows))
    assert all(out[i]["state"] == "matched" and out[i]["olid"] == 1000 + i for i in range(25))
    assert out[99]["state"] == "no_match"
    batched = [c for c in fake.calls if "q" in c]
    assert len(batched) == 3 and all(" OR " in c["q"] for c in batched[:2])
    assert len(fake.calls) - len(batched) >= 1                       # the miss still got its own ladder
    assert len(fake.calls) < 15                                      # vs ~27 one by one


def test_a_row_the_batch_misses_is_still_found_by_the_ladder():
    from lenny.core.bulk_import import matcher as m
    fake = FakeOL(*[work(f"Title {i}", [f"A {i}"], (500 + i, [], "eng")) for i in range(4)])
    real = fake.__call__
    def forgetful(params, retries=3, fresh=False):
        out = real(params, retries, fresh)
        return [w for w in out if w["title"] != "Title 2"] if "q" in params else out   # batch drops one
    with patch("lenny.core.bulk_import.matcher.search", forgetful):
        out = dict(m.match_rows([titled(i, f"Title {i}", f"A {i}") for i in range(4)]))
    assert out[2]["state"] == "matched" and out[2]["olid"] == 502


def test_batched_clauses_escape_quotes_and_skip_a_masthead():
    from lenny.core.bulk_import import matcher as m
    assert m._clause(titled(1, 'The "Quoted" Book', "Ann Writer")) == '(title:"The  Quoted  Book" AND author:"Ann Writer")'
    assert "author" not in m._clause(titled(1, "Digest", MASTHEAD))
    assert m._clause(dict(id=1, title=None, author=None, path="")) is None


def test_the_ladder_runs_several_searches_at_once():
    from lenny.core.bulk_import import matcher as m
    live, peak, lock = [0], [0], threading.Lock()
    def slow(params, retries=3, fresh=False):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return []
    with patch("lenny.core.bulk_import.matcher.search", slow):
        out = dict(m.match_rows([titled(i, f"Nothing {i}") for i in range(2)] * 1 + [titled(9, "x")]))   # < BATCH_MIN each → ladder
    assert len(out) == 3 and peak[0] >= 2 and peak[0] <= m._workers()


def test_workers_follow_the_allowed_rate():
    from lenny import configs
    from lenny.core.bulk_import import matcher as m
    assert m._workers() == 2                                                    # nobody signed in to Open Library
    with patch.object(configs, "OL_S3_ACCESS_KEY", "ak"), patch.object(configs, "OL_S3_SECRET_KEY", "sk"):
        assert m._workers() == 4                                                # signed in: paced at 3 a second


def test_an_upstream_failure_in_a_worker_stops_the_job_cleanly():
    from lenny.core.bulk_import import matcher as m
    with patch("lenny.core.bulk_import.matcher.search", side_effect=m.UpstreamError("down")):
        with pytest.raises(m.UpstreamError):
            list(m.match_rows([titled(1, "a"), titled(2, "b")]))


# ─── accuracy: language, publisher and year, and the edition lists ───────────

def cand(**kw):
    return dict(dict(olid=1, title="Dune", authors=["Frank Herbert"], isbns=[], language="eng", publisher=None,
                     year=None, work="/works/OL1W"), **kw)


def test_language_and_publisher_and_year_adjust_a_good_match():
    from lenny.core.bulk_import import matcher as m
    base = dict(title="Dune", author="Frank Herbert")
    plain = m.score(base, cand())
    assert plain == 100
    assert m.score(dict(base, language="fr"), cand()) < m.AUTO_MIN               # wrong-language edition
    assert m.score(dict(base, language="en-US"), cand()) == 100                  # en-US is eng
    near = dict(title="Dune: Messiah", author="Frank Herbert")                   # 86: review
    assert m.score(dict(near, language="en"), cand()) == 89 and m.score(dict(near, language="en"), cand()) < m.AUTO_MIN
    better = m.score(dict(near, publisher="Ace Books", year=1990), cand(publisher="Ace", year=1991))
    assert better > m.score(near, cand())


def test_extra_metadata_never_lifts_a_poor_match_or_makes_one_automatic():
    from lenny.core.bulk_import import matcher as m
    junk = dict(title="Cooking for Cats", author="Nobody", language="en", publisher="Ace", year=1990)
    assert m.score(junk, cand(publisher="Ace", year=1990)) < m.REVIEW_MIN
    review = dict(title="Dune: Messiah", author="Frank Herbert", language="en", publisher="Ace", year=1990)
    assert m.REVIEW_MIN <= m.score(review, cand(publisher="Ace", year=1990)) < m.AUTO_MIN


def test_publisher_may_be_part_of_the_other_name():
    from lenny.core.bulk_import import matcher as m
    assert m._same_publisher("Ace", "Ace Trade") and m._same_publisher("O'Reilly Media", "O'Reilly")
    assert not m._same_publisher("Ace", "Pace Books") and not m._same_publisher("", "Ace")


def test_edition_language_picks_between_editions_of_one_work():
    from lenny.core.bulk_import import matcher as m
    cands = [cand(olid=1, language="fre"), cand(olid=2, language="eng", publisher="Ace", year=1990), cand(olid=3, language="eng")]
    ranked = m.rank(dict(title="Dune", author="Frank Herbert", language="en", publisher="Ace", year=1990), cands)
    assert [c["olid"] for c in ranked] == [2, 3, 1]


def test_work_editions_parsing():
    from lenny.core.bulk_import import matcher as m
    body = {"entries": [
        {"key": "/books/OL7M", "title": "Dune", "publishers": ["Ace"], "publish_date": "Oct 1, 1990",
         "languages": [{"key": "/languages/eng"}], "isbn_13": ["9780441013593"], "isbn_10": ["0441013597"]},
        {"key": "/books/OL8M", "publish_date": "n.d.", "languages": None, "publishers": None},
        {"key": "/not/an/edition"}]}
    with patch("lenny.core.bulk_import.matcher._call", return_value=body):
        got = m.work_editions("/works/OL1W")
    assert [e["olid"] for e in got] == [7, 8]
    assert got[0] == {"olid": 7, "title": "Dune", "authors": [], "year": 1990, "publisher": "Ace", "language": "eng",
                      "isbn": "9780441013593", "isbns": ["9780441013593", "0441013597"], "work": "/works/OL1W", "format": None}
    assert got[1]["year"] is None and got[1]["language"] is None
    assert m.work_editions("../etc/passwd") == []                       # only real work keys are fetched


def test_the_change_edition_list_includes_editions_of_the_best_works(defector):
    from lenny.core.bulk_import import matcher as m
    fake = FakeOL(work("Dune", ["Frank Herbert"], (101, [], "fre")))
    editions = [dict(olid=102, title="Dune", authors=[], year=1990, publisher="Ace", language="eng", isbn="1", isbns=["1"], work="/works/OL1W"),
                dict(olid=101, title="Dune", authors=[], year=2001, publisher="Other", language="fre", isbn=None, isbns=[], work="/works/OL1W")]
    row = dict(isbn=None, title="Dune", author="Frank Herbert", language="en", publisher="Ace", year=1990, path=None)
    with patch("lenny.core.bulk_import.matcher.search", fake), \
         patch("lenny.core.bulk_import.matcher.work_editions", return_value=editions) as we:
        got = m.candidates_for(row)
    assert [c["olid"] for c in got] == [102, 101]                       # the English Ace 1990 edition first
    assert got[0]["authors"] == ["Frank Herbert"] and "work" not in got[0] and "isbns" not in got[0]
    assert we.call_count == 1


def test_a_failed_edition_lookup_keeps_the_rest_of_the_list(defector):
    from lenny.core.bulk_import import matcher as m
    row = dict(isbn=None, title="The Suitors", author="Ben Ehrenreich", language=None, path=None)
    with patch("lenny.core.bulk_import.matcher.work_editions", side_effect=m.UpstreamError("down")):
        assert [c["olid"] for c in m.candidates_for(row)] == [3413631]


# ─── asking Open Library ─────────────────────────────────────────────────────

def match(rowlist):
    from lenny.core.bulk_import import matcher as m
    return dict(m.match_rows(rowlist))


def test_isbns_go_fifty_to_a_query(ol):
    rl = [dict(id=i, isbn=f"{9780000000000 + i}", title=None, author=None, language=None) for i in range(120)]
    out = match(rl)
    assert len(ol.calls) == 3
    assert all(c["q"].startswith("isbn:(") and " OR " in c["q"] for c in ol.calls)
    assert len(out) == 120 and {u["state"] for u in out.values()} == {"no_match"}


def test_isbn_hit_is_matched_with_the_edition_that_carries_it(ol):
    out = match([dict(id=1, isbn="9780593099322", title=None, author=None, language=None)])
    assert out[1]["state"] == "matched" and out[1]["olid"] == 102 and out[1]["confidence"] == 100
    assert out[1]["match_title"] == "Dune" and out[1]["match_author"] == "Frank Herbert"


def test_unknown_isbn_falls_back_to_title_and_author(ol):
    out = match([dict(id=1, isbn="9789999999999", title="Dune", author="Frank Herbert", language="eng")])
    assert out[1]["state"] == "matched" and out[1]["olid"] == 101
    assert [("q" in c) for c in ol.calls] == [True, False]
    assert ol.calls[1]["author"] == "Frank Herbert" and ol.calls[1]["title"] == "Dune"


def test_rows_without_metadata_do_not_cost_a_request(ol):
    out = match([dict(id=1, isbn=None, title=None, author="Someone", language=None)])
    assert out[1]["state"] == "no_match" and "No ISBN or title" in out[1]["error"] and ol.calls == []


def test_one_query_per_title_row_and_results_arrive_as_they_come(ol):
    from lenny.core.bulk_import import matcher as m
    rl = [dict(id=1, isbn=None, title="Dune", author=None, language=None),
          dict(id=2, isbn=None, title="The Left Hand of Darkness", author="Le Guin, Ursula K.", language=None)]
    got = dict(m.match_rows(rl))
    assert got[1]["olid"] == 101 and got[2]["olid"] == 201


@pytest.fixture
def fresh_guard():
    from lenny.core import upstream
    with patch("lenny.core.bulk_import.matcher.open_library", upstream.Guard()), \
         patch("lenny.core.bulk_import.matcher.pacer", MagicMock()):
        yield


def resp(status=200, body=None, headers=None):
    r = MagicMock(status_code=status, headers=headers or {})
    r.json.return_value = body if body is not None else {"docs": []}
    r.raise_for_status.side_effect = None if status < 400 else __import__("requests").HTTPError("boom")
    return r


def test_429_waits_for_retry_after_then_succeeds(fresh_guard):
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", side_effect=[resp(429, headers={"Retry-After": "7"}), resp()]) as get, \
         patch("lenny.core.bulk_import.matcher.time.sleep") as sleep:
        assert m.search({"title": "x"}) == []
    assert get.call_count == 2 and sleep.call_args[0][0] == 7


def test_a_long_retry_after_is_reported_not_slept_through(fresh_guard):
    """The old behaviour slept up to a minute per try, silently."""
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", return_value=resp(429, headers={"Retry-After": "300"})) as get, \
         patch("lenny.core.bulk_import.matcher.time.sleep") as sleep:
        with pytest.raises(m.UpstreamError, match="about 300 seconds") as e:
            m.search({"title": "x"})
    assert get.call_count == 1 and sleep.call_count == 0 and e.value.retry_after == 300


def test_a_short_retry_after_is_waited_out_a_few_times_then_reported(fresh_guard):
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", return_value=resp(429, headers={"Retry-After": "5"})) as get, \
         patch("lenny.core.bulk_import.matcher.time.sleep") as sleep:
        with pytest.raises(m.UpstreamError, match="rate limiting"):
            m.search({"title": "x"})
    assert get.call_count == m.RETRIES + 1 and sleep.call_count == m.RETRIES
    assert all(c[0][0] <= m.MAX_RETRY_WAIT for c in sleep.call_args_list)


def test_open_library_down_is_an_upstream_error_and_trips_the_breaker(fresh_guard):
    import requests
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", side_effect=requests.ConnectionError("down")) as get:
        for _ in range(3):
            with pytest.raises(m.UpstreamError):
                m.search({"title": "x"})
        with pytest.raises(m.UpstreamError):
            m.search({"title": "x"})
        assert get.call_count == 3          # the 4th call never left


def test_requests_send_the_open_library_login_lenny_already_has(fresh_guard):
    """The matcher reuses the account the admin signed in with (ol_auth_headers), no setting of its own."""
    from lenny import configs
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", return_value=resp()) as get, \
         patch.object(configs, "OL_S3_ACCESS_KEY", "ak"), patch.object(configs, "OL_S3_SECRET_KEY", "sk"):
        m.search({"title": "x"})
    headers = get.call_args.kwargs["headers"]
    assert headers["User-Agent"] == configs.LENNY_HTTP_HEADERS["User-Agent"] and headers["Authorization"] == "LOW ak:sk"
    with patch("requests.get", return_value=resp()) as get, \
         patch.object(configs, "OL_S3_ACCESS_KEY", None), patch.object(configs, "OL_S3_SECRET_KEY", None):
        m.search({"title": "y"})
    assert "Authorization" not in get.call_args.kwargs["headers"]                      # signed out: anonymous


def test_interactive_calls_do_not_wait_out_a_429(fresh_guard):
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", return_value=resp(429, headers={"Retry-After": "30"})) as get, \
         patch("lenny.core.bulk_import.matcher.time.sleep") as sleep:
        with pytest.raises(m.UpstreamError):
            m.candidates_for(dict(title="Dune", isbn="9780441013593"))
        with pytest.raises(m.UpstreamError):
            m.lookup_edition(5)
    assert get.call_count == 2 and sleep.call_count == 0


def test_lookup_edition(fresh_guard):
    from lenny.core.bulk_import import matcher as m
    body = {"title": "Dune", "publishers": ["Ace"], "publish_date": "1990", "isbn_13": ["9780441013593"]}
    with patch("requests.get", side_effect=[resp(200, body), resp(404)]):
        assert m.lookup_edition(5) == {"olid": 5, "title": "Dune", "publisher": "Ace", "year": "1990", "isbn": "9780441013593"}
        assert m.lookup_edition(6) is None


# ─── pacing and limits ───────────────────────────────────────────────────────

def test_pacer_spaces_calls():
    from lenny.core.bulk_import.limits import Pacer
    now, slept = [100.0], []
    def sleep(s): slept.append(s); now[0] += s
    p = Pacer(rps=lambda: 2.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        p.wait()
    assert slept == [pytest.approx(0.5), pytest.approx(0.5)]


def test_pacer_holds_under_threads():
    from lenny.core.bulk_import.limits import Pacer
    p, stamps, lock = Pacer(rps=lambda: 100.0), [], threading.Lock()
    def go():
        for _ in range(5):
            p.wait()
            with lock: stamps.append(time.monotonic())
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(stamps) == 30
    assert max(stamps) - min(stamps) >= 0.9 * 29 / 100          # 30 calls at 100/s cannot finish faster


def test_limits_report_the_servers_numbers():
    from lenny import configs
    from lenny.core.bulk_import import limits
    anon = limits.limits()
    assert anon["ol_requests_per_second"] == 1.0 and anon["ol_identified"] is False and anon["ol_logged_in"] is False
    assert anon["formats"] == ["epub"] and anon["retention_days"] == 30 and anon["isbn_batch"] == 50
    assert anon["max_file_mb"] == 100 and anon["max_rows_per_call"] == 500 and anon["upload_concurrency"] == 2
    with patch.object(configs, "OL_S3_ACCESS_KEY", "ak"), patch.object(configs, "OL_S3_SECRET_KEY", "sk"):
        signed_in = limits.limits()                                             # the account Lenny already signs in with
    assert signed_in["ol_requests_per_second"] == 3.0 and signed_in["ol_identified"] is True and signed_in["ol_logged_in"] is True


# ─── the background job ──────────────────────────────────────────────────────

def run_job(batch_id, state="queued", row_ids=None):
    from lenny.core.bulk_import import jobs
    job = {"id": "t", "batch_id": batch_id, "status": "queued", "total": 0, "done": 0, "error": None}
    jobs._run(job, state, row_ids)
    return job


def batch_with(*rowdicts):
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    b.upsert_rows(bid, [dict(path=f"b{i}.epub", size=10 + i, mtime=1 + i, **r) for i, r in enumerate(rowdicts)])
    return bid


def states(bid):
    from lenny.core.bulk_import import batches as b
    return {r["title"] or r["isbn"] or r["path"]: r for r in b.list_rows(bid)["rows"]}


def test_job_matches_rows_and_saves_the_outcome(ol):
    bid = batch_with(dict(isbn="9780441013593"), dict(title="Dune", author="Frank Herbert"),
                     dict(title="Zzz Unknown Thing"), dict(author="No title"))
    job = run_job(bid)
    assert job["status"] == "done" and job["done"] == 4
    got = states(bid)
    assert got["9780441013593"]["state"] == "matched" and got["9780441013593"]["olid"] == 101
    assert got["Dune"]["auto_picked"] is True and got["Dune"]["decision"] == "auto"
    assert got["Zzz Unknown Thing"]["state"] == "no_match"
    assert got["b3.epub"]["state"] == "no_match"      # nothing but a meaningless file name


def test_job_never_touches_decided_skipped_or_settled_rows(ol):
    from lenny.core.bulk_import import batches as b
    bid = batch_with(*[dict(title="Dune", author="Frank Herbert") for _ in range(4)])
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    b.update_row(bid, ids[0], olid=999)                     # admin's choice
    b.update_row(bid, ids[1], skipped=True)
    set_state(bid, **{str(ids[2]): {"state": "imported", "olid": 5}})
    run_job(bid)
    r = {x["id"]: x for x in b.list_rows(bid)["rows"]}
    assert r[ids[0]]["olid"] == 999 and r[ids[0]]["decision"] == "user"
    assert r[ids[1]]["state"] == "queued" and r[ids[2]]["state"] == "imported" and r[ids[2]]["olid"] == 5
    assert r[ids[3]]["state"] == "matched"


def test_a_choice_made_while_the_job_runs_wins(ol):
    from lenny.core.bulk_import import batches as b
    bid = batch_with(dict(title="Dune", author="Frank Herbert"))
    row = b.list_rows(bid)["rows"][0]["id"]
    real = ol.__call__
    def search_then_admin_picks(params, retries=3, fresh=False):
        out = real(params, retries, fresh)
        b.update_row(bid, row, olid=777)                    # lands between the answer and the save
        return out
    with patch("lenny.core.bulk_import.matcher.search", search_then_admin_picks):
        run_job(bid)
    got = b.list_rows(bid)["rows"][0]
    assert got["olid"] == 777 and got["decision"] == "user" and got["state"] == "matched"


def test_recheck_only_retries_unmatched_rows(ol):
    from lenny.core.bulk_import import batches as b
    bid = batch_with(dict(title="Newly Added On OL"), dict(title="Dune", author="Frank Herbert"))
    run_job(bid)
    assert states(bid)["Newly Added On OL"]["state"] == "no_match"
    ol.works.append(work("Newly Added On OL", ["A"], (301, ["1234567890"], "eng")))
    ol.calls.clear()
    job = run_job(bid, state="no_match")
    assert states(bid)["Newly Added On OL"]["state"] == "matched" and states(bid)["Newly Added On OL"]["olid"] == 301
    assert len(ol.calls) == 1 and job["done"] == 1           # Dune was not asked again


def test_already_in_the_library_becomes_a_duplicate(ol):
    from lenny.core.db import session as db
    from lenny.core.models import Item, FormatEnum
    bid = batch_with(dict(isbn="9780441013593"), dict(isbn="9780441478125"))
    db.add(Item(openlibrary_edition=101, formats=FormatEnum.EPUB)); db.commit()
    try:
        run_job(bid)
        got = states(bid)
        assert got["9780441013593"]["state"] == "duplicate" and got["9780441478125"]["state"] == "matched"
    finally:
        db.query(Item).delete(); db.commit()


def test_open_library_failure_fails_the_job_and_keeps_rows_queued():
    from lenny.core.bulk_import import matcher as m
    bid = batch_with(dict(title="Dune"), dict(title="Other"))
    with patch("lenny.core.bulk_import.matcher.search", side_effect=m.UpstreamError("Open Library is not answering; try again shortly.")):
        job = run_job(bid)
    assert job["status"] == "failed" and "not answering" in job["error"]
    assert {r["state"] for r in states(bid).values()} == {"queued"}


def test_a_rate_limited_job_says_so_in_the_batch_summary(client):
    from lenny.core.bulk_import import jobs, matcher as m
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1, title="Dune")]})
    assert client.get(f"{API}/{bid}").json()["match"] is None
    err = m.UpstreamError("Open Library is rate limiting this server. Try again in about 90 seconds.", retry_after=90)
    with patch("lenny.core.bulk_import.matcher.search", side_effect=err), patch.object(jobs, "_executor", Inline()):
        client.post(f"{API}/{bid}/match")
    got = client.get(f"{API}/{bid}").json()
    assert got["match"]["status"] == "failed" and got["match"]["retry_after"] == 90 and "90 seconds" in got["match"]["error"]
    assert got["counts"] == {"queued": 1}                       # the row waits for a retry, it is not lost


def test_an_unexpected_error_does_not_leak_details():
    bid = batch_with(dict(title="Dune"))
    with patch("lenny.core.bulk_import.matcher.search", side_effect=RuntimeError("secret internals")):
        job = run_job(bid)
    assert job["status"] == "failed" and "secret" not in job["error"]


def test_partial_results_are_saved_as_the_job_goes(ol):
    from lenny.core.bulk_import import batches as b, jobs
    n = 35
    bid = batch_with(*[dict(title="Dune", author="Frank Herbert") for _ in range(n)])
    saves = []
    real_save = jobs._save
    def spying(batch_id, results, state):
        saves.append((len(results), b.get_summary(bid)["counts"].get("matched", 0)))
        return real_save(batch_id, results, state)
    with patch.object(jobs, "_save", spying), patch.object(jobs, "FLUSH_EVERY", 10):
        run_job(bid)
    assert len(saves) >= 3 and sum(k for k, _ in saves) == n         # saved in steps, not once at the end
    assert saves[1][1] > 0                                           # earlier steps were already visible
    assert b.get_summary(bid)["counts"]["matched"] == n


def test_more_rows_than_one_chunk(ol):
    from lenny.core.bulk_import import jobs
    n = jobs.CHUNK * 2 + 37
    from lenny.core.bulk_import import batches as b
    bid = new_batch()
    for i in range(0, n, 500):
        b.upsert_rows(bid, rows(min(500, n - i), start=i, title="Dune", author="Frank Herbert"))
    job = run_job(bid)
    assert job["done"] == n and b.get_summary(bid)["counts"] == {"matched": n}


def test_start_validates_and_runs_one_job_per_batch(ol):
    from lenny.core.bulk_import import batches as b, jobs
    bid = batch_with(dict(title="Dune"))
    for bad in (dict(state="matched"), dict(row_ids="x"), dict(row_ids=[True]), dict(row_ids=list(range(501)))):
        with pytest.raises(ValueError):
            jobs.start(bid, **bad)
    with pytest.raises(b.ImportBatchNotFound):
        jobs.start(bid + 999)
    with patch.object(jobs, "_executor", MagicMock()) as ex:
        first = jobs.start(bid)
        again = jobs.start(bid)
        assert first["id"] == again["id"] and first["total"] == 1 and ex.submit.call_count == 1
        jobs._jobs[first["id"]]["status"] = "done"
        assert jobs.start(bid)["id"] != first["id"]
    assert jobs.get(first["id"])["batch_id"] == bid and jobs.get("nope") is None


def test_row_ids_narrow_the_job(ol):
    from lenny.core.bulk_import import batches as b
    bid = batch_with(dict(title="Dune"), dict(title="Dune "), dict(title="Dune  "))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    run_job(bid, row_ids=ids[:1])
    assert [r["state"] for r in b.list_rows(bid)["rows"]] == ["matched", "queued", "queued"]


# ─── importing a row ─────────────────────────────────────────────────────────

def matched_row(**extra):
    from lenny.core.bulk_import import batches as b
    bid = batch_with(dict(title="Dune", author="Frank Herbert", isbn="9780441013593"))
    rid = b.list_rows(bid)["rows"][0]["id"]
    set_state(bid, **{str(rid): dict(state="matched", olid=101, **extra)})
    return bid, rid


def do_import(bid, rid, add=None):
    from lenny.core.api import LennyAPI
    from lenny.core.bulk_import import importer
    with patch.object(LennyAPI, "add", add or MagicMock()) as m:
        return importer.import_row(bid, rid, MagicMock()), m


def test_import_stores_the_book_and_drops_matching_metadata():
    bid, rid = matched_row(encrypted=True, match_title="Dune")
    row, add = do_import(bid, rid)
    assert row["state"] == "imported" and row["olid"] == 101 and row["match_title"] == "Dune"
    assert row["title"] is None and row["isbn"] is None and row["author"] is None
    assert add.call_args.args[0] == 101 and add.call_args.kwargs["encrypt"] is True


def test_importing_twice_stores_once():
    bid, rid = matched_row()
    do_import(bid, rid)
    row, add = do_import(bid, rid)
    assert row["state"] == "imported" and add.call_count == 0


@pytest.mark.parametrize("error, state", [
    ("ItemExistsError", "duplicate"), ("InvalidFileError", "not_epub"),
    ("FileTooLargeError", "failed"), ("S3UploadError", "failed"), ("RuntimeError", "failed"),
])
def test_import_outcomes(error, state):
    from lenny.core import exceptions
    exc = getattr(exceptions, error, RuntimeError)("sensitive detail /srv/path")
    bid, rid = matched_row()
    row, _ = do_import(bid, rid, MagicMock(side_effect=exc))
    assert row["state"] == state
    assert "sensitive" not in (row["error"] or "") or error in ("InvalidFileError", "FileTooLargeError")


def test_failed_rows_can_be_retried():
    bid, rid = matched_row()
    row, _ = do_import(bid, rid, MagicMock(side_effect=RuntimeError("x")))
    assert row["state"] == "failed"
    row, _ = do_import(bid, rid)
    assert row["state"] == "imported"


def test_a_live_claim_is_busy_and_an_abandoned_one_is_retaken():
    from datetime import datetime, timedelta, timezone
    from lenny.core.bulk_import import batches as b, importer
    bid, rid = matched_row()
    now = datetime.now(timezone.utc)
    set_state(bid, **{str(rid): dict(state="importing", claimed_at=now)})
    with pytest.raises(b.ImportRowBusy):
        do_import(bid, rid)
    set_state(bid, **{str(rid): dict(claimed_at=now - importer.CLAIM_TTL - timedelta(seconds=1))})
    row, add = do_import(bid, rid)                              # as left by a crash
    assert row["state"] == "imported" and add.call_count == 1


def test_a_late_finish_does_not_overwrite_a_settled_row():
    """Attempt A is slow; its claim expires, B imports the book, then A's own add reports 'exists'."""
    from lenny.core.api import LennyAPI
    from lenny.core.bulk_import import batches as b, importer
    from lenny.core.exceptions import ItemExistsError
    bid, rid = matched_row()
    def slow_add(*a, **k):
        set_state(bid, **{str(rid): dict(state="imported", claimed_at=None)})   # B won meanwhile
        raise ItemExistsError("exists")
    with patch.object(LennyAPI, "add", slow_add):
        row = importer.import_row(bid, rid, MagicMock())
    assert row["state"] == "imported"


@pytest.mark.parametrize("fields", [dict(state="needs_review", olid=None), dict(skipped=True), dict(state="queued"), dict(state="no_match")])
def test_only_matched_unskipped_rows_import(fields):
    from lenny.core.bulk_import import batches as b
    bid, rid = matched_row()
    set_state(bid, **{str(rid): fields})
    with pytest.raises(ValueError):
        do_import(bid, rid)
    with pytest.raises(b.ImportRowNotFound):
        do_import(bid, rid + 999)


def test_concurrent_imports_of_one_edition_store_it_once(pg):
    """Two rows (two copies of the same book) race to import; the edition lock lets one win."""
    from lenny.core.api import LennyAPI
    from lenny.core.bulk_import import batches as b, importer
    from lenny.core.db import session
    from lenny.core.models import FormatEnum, Item
    bid = b.create_batch()["id"]
    b.upsert_rows(bid, rows(6, title="Dune"))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(i): dict(state="matched", olid=101) for i in ids})
    session.remove()
    out, barrier = [], threading.Barrier(len(ids))
    def worker(rid):
        try:
            barrier.wait()
            out.append(importer.import_row(bid, rid, MagicMock())["state"])
        finally:
            session.remove()
    with patch.object(LennyAPI, "upload_files", return_value=FormatEnum.EPUB.value):
        ts = [threading.Thread(target=worker, args=(i,)) for i in ids]
        [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(out) == ["duplicate"] * 5 + ["imported"]
    assert session.query(Item).filter(Item.openlibrary_edition == 101).count() == 1


# ─── routes ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path", [
    ("post", f"{API}/1/match"), ("get", f"{IMPORT}/match/abc"), ("get", f"{API}/1/rows/1/candidates"),
    ("get", f"{IMPORT}/editions/lookup?key=OL1M"), ("post", f"{IMPORT}/check-duplicates"),
    ("get", f"{IMPORT}/limits"), ("post", f"{API}/1/rows/1/file"),
])
def test_new_routes_require_the_admin_credentials(app_module, method, path):
    from fastapi.testclient import TestClient
    kwargs = {"files": {"file": ("a.epub", b"x")}} if path.endswith("/file") else {"json": {"olids": []}}
    assert TestClient(app_module).request(method.upper(), path, **kwargs).status_code == 403


class Inline:
    """Runs a submitted job at once, so a route test can read its result."""
    def submit(self, fn, *a): fn(*a)


def test_match_flow_over_http(client, ol):
    from lenny.core.bulk_import import jobs
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [
        dict(path="a.epub", size=1, mtime=1, title="Dune", author="Frank Herbert"),
        dict(path="b.epub", size=2, mtime=2, title="Nothing Like It")]})
    with patch.object(jobs, "_executor", Inline()):
        job = client.post(f"{API}/{bid}/match")
    assert job.status_code == 202 and job.json()["total"] == 2
    got = client.get(f"{IMPORT}/match/{job.json()['id']}").json()
    assert got["status"] == "done" and got["done"] == 2
    assert client.get(f"{API}/{bid}").json()["counts"] == {"matched": 1, "no_match": 1}
    assert client.get(f"{IMPORT}/match/unknown").status_code == 404

    row = client.get(f"{API}/{bid}/rows", params={"state": "matched"}).json()["rows"][0]
    cands = client.get(f"{API}/{bid}/rows/{row['id']}/candidates").json()["candidates"]
    assert cands[0]["olid"] == 101 and cands[0]["score"] == 100 and "isbns" not in cands[0]
    assert set(cands[0]) == {"olid", "title", "authors", "year", "publisher", "language", "isbn", "score", "in_library"}
    assert client.get(f"{API}/{bid}/rows/99999/candidates").status_code == 404

    assert client.post(f"{API}/{bid}/match", json={"state": "bogus"}).status_code == 400
    assert client.post(f"{API}/{bid + 99}/match").status_code == 404


def test_upstream_trouble_is_a_502_not_a_500(client):
    from lenny.core.bulk_import import matcher as m
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1, title="Dune")]})
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    with patch("lenny.core.bulk_import.matcher.search", side_effect=m.UpstreamError("Open Library is not answering")):
        r = client.get(f"{API}/{bid}/rows/{rid}/candidates")
    assert r.status_code == 502 and "not answering" in r.json()["detail"]


def test_lookup_check_duplicates_and_limits_over_http(client):
    from lenny.core.db import session as db
    from lenny.core.models import FormatEnum, Item
    with patch("lenny.core.bulk_import.matcher.lookup_edition", return_value={"olid": 101, "title": "Dune"}):
        for key in ("OL101M", "/books/OL101M", " ol101m "):
            assert client.get(f"{IMPORT}/editions/lookup", params={"key": key}).json()["title"] == "Dune"
    for bad in ("101", "OL101W", "../x", ""):
        assert client.get(f"{IMPORT}/editions/lookup", params={"key": bad}).status_code == 400
    with patch("lenny.core.bulk_import.matcher.lookup_edition", return_value=None):
        assert client.get(f"{IMPORT}/editions/lookup", params={"key": "OL5M"}).status_code == 404

    db.add(Item(openlibrary_edition=101, formats=FormatEnum.EPUB)); db.commit()
    try:
        with patch("lenny.core.bulk_import.matcher.lookup_edition", return_value={"olid": 101, "title": "Dune"}):
            assert client.get(f"{IMPORT}/editions/lookup", params={"key": "OL101M"}).json()["in_library"] is True
        assert client.post(f"{IMPORT}/check-duplicates", json={"olids": [101, 102]}).json() == {"existing": [101]}
    finally:
        db.query(Item).delete(); db.commit()
    assert client.post(f"{IMPORT}/check-duplicates", json={"olids": []}).json() == {"existing": []}
    for bad in ({}, {"olids": "1"}, {"olids": [True]}, {"olids": [0]}, {"olids": list(range(1, 1002))}):
        assert client.post(f"{IMPORT}/check-duplicates", json=bad).status_code == 400
    assert client.get(f"{IMPORT}/limits").json()["formats"] == ["epub"]


def test_import_file_over_http(client):
    from lenny.core.api import LennyAPI
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=3, mtime=1, title="Dune")]})
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    files = {"file": ("a.epub", io.BytesIO(b"abc"), "application/epub+zip")}
    assert client.post(f"{API}/{bid}/rows/{rid}/file", files=files).status_code == 400   # not matched yet
    client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 101})
    with patch.object(LennyAPI, "add") as add:
        r = client.post(f"{API}/{bid}/rows/{rid}/file", files={"file": ("a.epub", io.BytesIO(b"abc"), "application/epub+zip")})
    assert r.status_code == 200 and r.json()["state"] == "imported" and add.call_args.args[0] == 101
    assert client.post(f"{API}/{bid}/rows/9999/file", files={"file": ("a.epub", io.BytesIO(b"abc"))}).status_code == 404


@pytest.mark.anyio
async def test_import_uploads_run_under_the_upload_limiter(app_module, admin_headers):
    """A page firing many uploads at once cannot run more than the limit together."""
    import asyncio
    import anyio
    import httpx
    live, peak, lock = [0], [0], threading.Lock()

    def slow_import(batch_id, row_id, upload):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.1)
        with lock:
            live[0] -= 1
        return {"id": row_id, "state": "imported"}

    async def send(client, rid):
        r = await client.post(f"{API}/1/rows/{rid}/file", headers=admin_headers,
                              files={"file": ("a.epub", io.BytesIO(b"abc"))})
        return r.json()["state"]

    transport = httpx.ASGITransport(app=app_module)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        with patch("lenny.routes.bulk_import.importer.import_row", slow_import), \
             patch("lenny.routes.bulk_import._UPLOAD_LIMITER", anyio.CapacityLimiter(2)):
            out = await asyncio.gather(*[send(client, r) for r in range(6)])
    assert out == ["imported"] * 6 and peak[0] == 2


# ─── importing a staged row: no upload, no browser ───────────────────────────

@pytest.fixture
def staged_s3():
    """An object store that remembers copies and deletes, for the staged-import path."""
    from unittest.mock import MagicMock
    store = MagicMock()
    store.BOOKSHELF_BUCKET = "bookshelf"
    store.objects = {}
    def copy_object(Bucket, CopySource, Key):
        from botocore.exceptions import ClientError
        if CopySource["Key"] not in store.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "CopyObject")
        store.objects[Key] = store.objects[CopySource["Key"]]
    store.copy_object.side_effect = copy_object
    store.delete_object.side_effect = lambda Bucket, Key: store.objects.pop(Key, None)
    store.get_keys.side_effect = lambda prefix="", **k: [o for o in sorted(store.objects) if o.startswith(prefix)]
    with patch("lenny.core.bulk_import.staging.s3", store), patch("lenny.core.api.s3", store):
        yield store


def staged_row(store, **extra):
    from lenny.core.bulk_import import batches as b, staging
    bid, rid = matched_row(**extra)
    set_state(bid, **{str(rid): dict(staged=True)})
    store.objects[staging.key(bid, rid)] = b"epub"
    return bid, rid


def test_a_staged_book_imports_with_no_upload_and_the_copy_is_cleaned_up(staged_s3, db_items):
    from lenny.core.bulk_import import importer, staging
    from lenny.core.models import Item
    bid, rid = staged_row(staged_s3, encrypted=True, match_title="Dune")
    row = importer.import_staged(bid, rid)
    assert row["state"] == "imported" and row["staged"] is False and row["match_title"] == "Dune" and row["title"] is None
    assert "101.epub" in staged_s3.objects and staging.key(bid, rid) not in staged_s3.objects
    item = db_items.query(Item).filter(Item.openlibrary_edition == 101).one()
    assert item.encrypted is True
    assert importer.import_staged(bid, rid)["state"] == "imported"           # retry is harmless


def test_a_staged_book_already_in_the_library_is_a_duplicate_and_the_copy_goes(staged_s3, db_items):
    from lenny.core.bulk_import import importer, staging
    from lenny.core.models import FormatEnum, Item
    db_items.add(Item(openlibrary_edition=101, formats=FormatEnum.EPUB)); db_items.commit()
    bid, rid = staged_row(staged_s3)
    row = importer.import_staged(bid, rid)
    assert row["state"] == "duplicate" and row["staged"] is False and staging.key(bid, rid) not in staged_s3.objects


def test_a_vanished_staged_copy_asks_for_the_file_again(staged_s3, db_items):
    from lenny.core.bulk_import import importer, staging
    bid, rid = staged_row(staged_s3)
    staged_s3.objects.pop(staging.key(bid, rid))
    row = importer.import_staged(bid, rid)
    assert row["state"] == "failed" and row["staged"] is False and "send the file again" in row["error"]


def test_a_store_failure_keeps_the_staged_copy_for_a_retry(staged_s3, db_items):
    from botocore.exceptions import ClientError
    from lenny.core.bulk_import import importer, staging
    bid, rid = staged_row(staged_s3)
    good = staged_s3.copy_object.side_effect
    staged_s3.copy_object.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "CopyObject")
    row = importer.import_staged(bid, rid)
    assert row["state"] == "failed" and row["staged"] is True and staging.key(bid, rid) in staged_s3.objects
    staged_s3.copy_object.side_effect = good
    assert importer.import_staged(bid, rid)["state"] == "imported"


def test_a_database_failure_removes_the_copy_it_made(staged_s3, db_items):
    from lenny.core.bulk_import import importer
    from lenny.core import api
    from lenny.core.models import Item
    bid, rid = staged_row(staged_s3)
    real_commit = api.db.commit
    def commit(*a, **k):
        if any(isinstance(o, Item) for o in api.db.new):          # only the new library row fails
            raise RuntimeError("db down")
        return real_commit(*a, **k)
    with patch.object(api.db, "commit", commit):
        row = importer.import_staged(bid, rid)
    assert row["state"] == "failed" and "101.epub" not in staged_s3.objects


def test_only_staged_matched_rows_can_be_imported_that_way(staged_s3, db_items):
    from lenny.core.bulk_import import importer
    bid, rid = matched_row()                                   # matched but its file never sent
    with pytest.raises(ValueError, match="not been uploaded"):
        importer.import_staged(bid, rid)
    set_state(bid, **{str(rid): dict(staged=True, skipped=True)})
    with pytest.raises(ValueError):
        importer.import_staged(bid, rid)


@pytest.fixture
def db_items():
    from lenny.core.cache import CacheEntry
    from lenny.core.db import Base, engine, session
    from lenny.core.models import Item
    Base.metadata.create_all(engine)
    session.remove()
    yield session
    session.rollback()
    session.query(Item).delete(); session.query(CacheEntry).delete(); session.commit()
    session.remove()


def test_staged_import_over_http_and_it_never_needs_the_browser_again(client, staged_s3, db_items):
    import io
    from lenny.core.bulk_import import staging
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=3, mtime=1, title="Dune")]})
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    assert client.post(f"{API}/{bid}/rows/{rid}/import").status_code == 400          # nothing staged, not matched
    client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 101})
    assert client.post(f"{API}/{bid}/rows/{rid}/import").status_code == 400          # matched, file not sent
    epub = __import__("tests.bulk_import.test_batches", fromlist=["epub_upload"]).epub_upload().file.read()
    staged_s3.upload_fileobj.side_effect = lambda f, bucket, key, **k: staged_s3.objects.__setitem__(key, f.read())
    assert client.put(f"{API}/{bid}/rows/{rid}/staged", files={"file": ("a.epub", io.BytesIO(epub))}).json()["staged"] is True
    r = client.post(f"{API}/{bid}/rows/{rid}/import")                                # no file, no browser
    assert r.status_code == 200 and r.json()["state"] == "imported"
    assert staged_s3.objects.get("101.epub") == epub and staging.key(bid, rid) not in staged_s3.objects


def test_the_staged_import_route_requires_admin(app_module):
    from fastapi.testclient import TestClient
    assert TestClient(app_module).post(f"{API}/1/rows/1/import").status_code == 403


def test_two_staged_copies_of_one_edition_import_once(pg, staged_s3):
    """Two rows (two copies of the same book) finish at the same instant; the edition lock lets one win."""
    from lenny.core.bulk_import import batches as b, importer, staging
    from lenny.core.db import session
    from lenny.core.models import Item
    bid = b.create_batch()["id"]
    b.upsert_rows(bid, rows(6, title="Dune"))
    ids = [r["id"] for r in b.list_rows(bid)["rows"]]
    set_state(bid, **{str(i): dict(state="matched", olid=101, staged=True) for i in ids})
    for i in ids:
        staged_s3.objects[staging.key(bid, i)] = b"epub"
    session.remove()
    out, barrier = [], threading.Barrier(len(ids))
    def worker(rid):
        try:
            barrier.wait()
            out.append(importer.import_staged(bid, rid)["state"])
        finally:
            session.remove()
    ts = [threading.Thread(target=worker, args=(i,)) for i in ids]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sorted(out) == ["duplicate"] * 5 + ["imported"]
    assert session.query(Item).filter(Item.openlibrary_edition == 101).count() == 1
    assert [k for k in staged_s3.objects if k.startswith("import-staging/")] == []


# ─── the "find it yourself" box ──────────────────────────────────────────────

def one_row_batch(client, **row):
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1, **row)]})
    return bid, client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]


def test_search_by_words_returns_scored_candidates_with_library_flag(client, ol, db_items):
    from lenny.core.models import FormatEnum, Item
    db_items.add(Item(openlibrary_edition=101, formats=FormatEnum.EPUB)); db_items.commit()
    bid, rid = one_row_batch(client, title="something else")
    got = client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": "dune"}).json()["candidates"]
    assert [(c["olid"], c["in_library"]) for c in got][:1] == [(101, True)] or got[0]["olid"] in (101, 102)
    assert {"olid", "title", "authors", "year", "publisher", "language", "isbn", "score", "in_library"} <= set(got[0])
    assert "isbns" not in got[0] and "work" not in got[0]
    assert ol.calls[-1]["q"] == "dune"                                          # live words, as typed


def test_search_accepts_a_pasted_key_or_link_and_an_isbn(client, ol):
    bid, rid = one_row_batch(client, title="Dune")
    edition = {"olid": 101, "title": "Dune", "publisher": "Ace", "year": "Oct 1, 1990", "isbn": "9780441013593"}
    for q in ("OL101M", "https://openlibrary.org/books/OL101M/Dune", "/books/ol101m"):
        with patch("lenny.core.bulk_import.matcher.lookup_edition", return_value=edition) as lookup:
            got = client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": q}).json()["candidates"]
        assert [c["olid"] for c in got] == [101] and got[0]["year"] == 1990 and lookup.call_args.args == (101,)
    with patch("lenny.core.bulk_import.matcher.lookup_edition", return_value=None):
        assert client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": "OL5M"}).json() == {"candidates": []}
    client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": "978-0-441-01359-3"})
    assert ol.calls[-1]["q"] == "isbn:9780441013593"                            # an ISBN, dashes and all


def test_search_ranks_by_the_row_so_the_right_language_comes_first(client):
    fake = FakeOL(work("Dune", ["Frank Herbert"], (1, [], "fre")), work("Dune", ["Frank Herbert"], (2, [], "eng")))
    with patch("lenny.core.bulk_import.matcher.search", fake):
        bid, rid = one_row_batch(client, title="Dune", author="Frank Herbert", language="en")
        with patch("lenny.core.bulk_import.matcher.search", lambda *a, **k: fake.works):
            got = client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": "dune"}).json()["candidates"]
    assert [c["olid"] for c in got] == [2, 1]


def test_search_input_is_checked_and_errors_are_clean(client, ol):
    from lenny.core.bulk_import import matcher as m
    bid, rid = one_row_batch(client, title="Dune")
    assert client.get(f"{API}/{bid}/rows/{rid}/search").status_code == 400
    assert client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": " a "}).status_code == 400
    assert client.get(f"{API}/{bid}/rows/999/search", params={"q": "dune"}).status_code == 404
    with patch("lenny.core.bulk_import.matcher.search", side_effect=m.UpstreamError("down", retry_after=30)):
        r = client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": "dune"})
    assert r.status_code == 502
    long = "x" * 500
    client.get(f"{API}/{bid}/rows/{rid}/search", params={"q": long})
    assert len(ol.calls[-1]["q"]) <= 200                                         # never an unbounded query


def test_candidates_also_carry_the_library_flag(client, ol, db_items):
    bid, rid = one_row_batch(client, title="Dune", author="Frank Herbert")
    got = client.get(f"{API}/{bid}/rows/{rid}/candidates").json()["candidates"]
    assert got and all(c["in_library"] is False for c in got)


def test_the_search_route_requires_admin(app_module):
    from fastapi.testclient import TestClient
    assert TestClient(app_module).get(f"{API}/1/rows/1/search", params={"q": "dune"}).status_code == 403


# ─── imported rows: a timestamp, and the file can come back as a new row ──────

def test_an_imported_row_is_stamped_and_its_file_can_be_dropped_again(db_items):
    from datetime import datetime, timezone
    from lenny.core.bulk_import import batches as b
    bid, rid = matched_row()
    before = datetime.now(timezone.utc)
    row, _ = do_import(bid, rid)
    assert row["state"] == "imported" and datetime.fromisoformat(str(row["imported_at"])).replace(tzinfo=timezone.utc) >= before.replace(microsecond=0)
    again = b.upsert_rows(bid, [dict(path="lib/book0.epub", size=1000, mtime=1_700_000_000_000)])     # same file as matched_row's
    assert again == {"created": 1, "existing": 0, "existing_states": {}}
    got = b.list_rows(bid, offset=0)
    assert got["total"] == 2
    states = sorted(r["state"] for r in got["rows"])
    assert states == ["imported", "queued"]
    fresh = [r for r in got["rows"] if r["state"] == "queued"][0]
    assert fresh["olid"] is None and fresh["imported_at"] is None and fresh["decision"] == "auto"      # nothing carried over
    # and dropping it a third time attaches to the queued one, not a third row
    assert b.upsert_rows(bid, [dict(path="lib/book0.epub", size=1000, mtime=1_700_000_000_000)])["existing"] == 1


def test_a_failed_or_duplicate_row_is_not_stamped(db_items):
    from lenny.core.bulk_import import batches as b
    bid, rid = matched_row()
    row, _ = do_import(bid, rid, MagicMock(side_effect=RuntimeError("x")))
    assert row["state"] == "failed" and row["imported_at"] is None
    bid2, rid2 = matched_row()
    from lenny.core.exceptions import ItemExistsError
    row2, _ = do_import(bid2, rid2, MagicMock(side_effect=ItemExistsError("x")))
    assert row2["state"] == "duplicate" and row2["imported_at"] is None


# ─── choosing another edition must really change the row ─────────────────────

def auto_matched_row(client, staged=False):
    """A row auto-matched to the wrong book (Game Show Zine, 76%), as in the bug report."""
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1, title="Defector Weekly Digest")]})
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    set_state(bid, **{str(rid): dict(state="matched", olid=5, auto_picked=True, confidence=76, candidates_count=6,
                                    match_title="Game Show Zine", match_author="Someone Else", match_year=1999, staged=staged)})
    return bid, rid


def test_picking_another_edition_updates_the_response_and_the_next_list(client):
    bid, rid = auto_matched_row(client, staged=True)
    facts = {"olid": 62576958, "title": "Defector Weekly Digest: May 17-23, 2026", "authors": ["Tom Ley", "Diana Moskovitz"],
             "year": 2026, "publisher": "The Brick House Cooperative", "language": "eng", "isbn": None}
    with patch("lenny.core.bulk_import.matcher.edition_facts", return_value=facts) as lookup:
        r = client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 62576958})
    assert r.status_code == 200 and lookup.call_args.args == (62576958,)
    row = r.json()
    assert (row["olid"], row["state"], row["decision"], row["auto_picked"]) == (62576958, "matched", "user", False)
    assert (row["match_title"], row["match_author"], row["match_year"]) == (
        "Defector Weekly Digest: May 17-23, 2026", "Tom Ley, Diana Moskovitz", 2026)
    assert row["confidence"] == 0 and row["candidates_count"] == 6 and row["staged"] is True
    listed = client.get(f"{API}/{bid}/rows").json()["rows"][0]
    assert (listed["olid"], listed["match_title"], listed["match_year"]) == (62576958, row["match_title"], 2026)


def test_a_failed_lookup_leaves_the_row_exactly_as_it_was(client):
    from lenny.core.bulk_import import matcher as m
    bid, rid = auto_matched_row(client)
    before = client.get(f"{API}/{bid}/rows").json()["rows"][0]
    with patch("lenny.core.bulk_import.matcher.edition_facts", side_effect=m.UpstreamError("Open Library is not answering", retry_after=None)):
        r = client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 62576958})
    assert r.status_code == 502 and "not answering" in r.json()["detail"]
    assert client.get(f"{API}/{bid}/rows").json()["rows"][0] == before
    with patch("lenny.core.bulk_import.matcher.edition_facts", return_value=None):
        r = client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 62576958})
    assert r.status_code == 400 and "no edition" in r.json()["detail"]
    assert client.get(f"{API}/{bid}/rows").json()["rows"][0] == before


@pytest.mark.parametrize("state", ["matched", "needs_review", "no_match", "failed", "duplicate"])
def test_the_choice_works_in_every_state_but_importing(client, state):
    bid, rid = auto_matched_row(client)
    set_state(bid, **{str(rid): dict(state=state)})
    assert client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 7}).status_code == 200
    got = client.get(f"{API}/{bid}/rows").json()["rows"][0]
    assert got["olid"] == 7 and got["match_title"] == "Edition 7" and got["state"] == "matched"   # a duplicate re-opens with the new edition


def test_a_row_being_imported_refuses_the_change_with_a_409(client):
    bid, rid = auto_matched_row(client)
    set_state(bid, **{str(rid): dict(state="importing")})
    assert client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 7}).status_code == 409
    assert client.get(f"{API}/{bid}/rows").json()["rows"][0]["olid"] == 5


def test_a_choice_made_during_a_match_job_wins_and_keeps_its_details(ol, client):
    from lenny.core.bulk_import import jobs
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path="a.epub", size=1, mtime=1, title="Dune", author="Frank Herbert")]})
    rid = client.get(f"{API}/{bid}/rows").json()["rows"][0]["id"]
    real = ol.__call__
    def search_then_admin_picks(params, retries=3, fresh=False):
        out = real(params, retries, fresh)
        client.patch(f"{API}/{bid}/rows/{rid}", json={"olid": 777})                  # lands before the job saves
        return out
    with patch("lenny.core.bulk_import.matcher.search", search_then_admin_picks), patch.object(jobs, "_executor", Inline()):
        client.post(f"{API}/{bid}/match")
    got = client.get(f"{API}/{bid}/rows").json()["rows"][0]
    assert (got["olid"], got["match_title"], got["decision"]) == (777, "Edition 777", "user")


def test_automatic_matches_carry_the_editions_year_too(ol):
    from lenny.core.bulk_import import matcher as m
    out = dict(m.match_rows([titled(1, "Dune", "Frank Herbert")]))
    assert out[1]["match_year"] == 2000 and out[1]["match_title"] == "Dune"


@pytest.mark.real_edition_lookup
def test_edition_facts_come_from_one_key_search_or_the_record_itself(fresh_guard):
    from lenny.core.bulk_import import matcher as m
    body = {"docs": [{"key": "/works/OL1W", "title": "Defector Weekly Digest", "author_name": ["Tom Ley"],
                      "editions": {"docs": [{"key": "/books/OL62576958M", "title": "Defector Weekly Digest: May 17-23, 2026",
                                             "publisher": ["Brick House"], "language": ["eng"], "publish_year": [2026]}]}}]}
    with patch("requests.get", return_value=resp(200, body)) as get:
        facts = m.edition_facts(62576958)
    assert facts["title"] == "Defector Weekly Digest: May 17-23, 2026" and facts["authors"] == ["Tom Ley"] and facts["year"] == 2026
    assert get.call_args.kwargs["params"]["q"] == "edition_key:OL62576958M" and get.call_count == 1
    record = {"title": "Plain Title", "publishers": ["Ace"], "publish_date": "1990", "isbn_13": ["9780441013593"]}
    with patch("requests.get", side_effect=[resp(200, {"docs": []}), resp(200, record)]):
        facts = m.edition_facts(5)                                                    # not in the search index: use the record
    assert facts["title"] == "Plain Title" and facts["year"] == 1990
    with patch("requests.get", side_effect=[resp(200, {"docs": []}), resp(404)]):
        assert m.edition_facts(6) is None


@pytest.mark.real_edition_lookup
def test_edition_facts_raise_when_open_library_is_down(fresh_guard):
    import requests
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", side_effect=requests.ConnectionError("down")):
        with pytest.raises(m.UpstreamError):
            m.edition_facts(5)


# ─── the exact edition: every ISBN, the year, the publisher ──────────────────

def test_isbn_forms_convert_between_10_and_13():
    from lenny.core.bulk_import import matcher as m
    assert m.isbn_forms("0441013597") == {"0441013597", "9780441013593"}
    assert m.isbn_forms("9780441013593") == {"9780441013593", "0441013597"}
    assert m.isbn_forms("9791000000000") == {"9791000000000"}                          # 979- has no 10-digit form
    assert m.isbn_forms("12345") == set() and m.isbn_forms(None) == set()
    assert m.isbn_forms("080442957X") == {"080442957X", "9780804429573"}           # an X check digit


def test_any_of_a_files_isbns_identifies_the_edition(ol):
    from lenny.core.bulk_import import matcher as m
    row = dict(id=1, isbn="9789999999999", isbn_alt="0441013597 9780140328721", title=None, author=None,
               language=None, publisher=None, year=None, path=None)
    out = dict(m.match_rows([row]))
    assert out[1]["state"] == "matched" and out[1]["olid"] == 101 and out[1]["confidence"] == 100   # the second ISBN hit (ISBN-10 form)
    assert "isbn:(" in ol.calls[0]["q"] and "0441013597" in ol.calls[0]["q"] and "9789999999999" in ol.calls[0]["q"]
    assert "9780441013593" not in ol.calls[0]["q"]                                                 # forms are not sent twice


def test_a_row_with_only_alternate_isbns_is_still_matched_by_them(ol):
    from lenny.core.bulk_import import matcher as m
    row = dict(id=1, isbn=None, isbn_alt="9780140328721", title=None, author=None, language=None, publisher=None, year=None, path=None)
    assert m._usable(row) and m.row_isbns(row) >= {"9780140328721"}


def editions_of_dune():
    mk = lambda olid, year, pub, lang, fmt: dict(olid=olid, title="Dune", authors=[], year=year, publisher=pub, language=lang,
                                                 isbn=f"97800000{olid:05d}", isbns=[f"97800000{olid:05d}"], work="/works/OL1W", format=fmt)
    return [mk(1, 1965, "Chilton", "eng", "Hardcover"), mk(2, 1990, "Ace", "eng", "Paperback"),
            mk(3, 2019, "Ace", "eng", "Paperback"), mk(4, 2019, "Penguin", "eng", "Ebook"), mk(5, 2019, "Ace", "fre", "Ebook")]


class TargetedOL(FakeOL):
    """FakeOL that also answers the refine step's `(title:"T" AND author:"A") AND publisher:"P" AND publish_year:Y [AND language:L]`
    from a list of edition dicts, the way Open Library returns the matching edition in the work."""

    def __init__(self, *works, editions=()):
        super().__init__(*works)
        self.editions = list(editions)

    def __call__(self, params, retries=3, fresh=False):
        q = params.get("q", "")
        if "publisher:" not in q and "publish_year:" not in q:
            return super().__call__(params, retries, fresh)
        import re
        self.calls.append(dict(params))
        pub = re.search(r'publisher:"([^"]*)"', q)
        year = re.search(r"publish_year:(\d+)", q)
        lang = re.search(r"language:(\w+)", q)
        for e in self.editions:
            if pub and pub.group(1).lower() not in (e["publisher"] or "").lower():
                continue
            if year and int(year.group(1)) != e["year"]:
                continue
            if lang and lang.group(1) != e["language"]:
                continue
            return [{"key": "/works/OL1W", "title": "Dune", "author_name": ["Frank Herbert"],
                     "editions": {"docs": [{"key": f"/books/OL{e['olid']}M", "title": "Dune", "publisher": [e["publisher"]],
                                            "language": [e["language"]], "publish_year": [e["year"]], "isbn": []}]}}]
        return []


def refined(row_extra, editions=None, search_olid=1, targeted=False):
    """Match one Dune row whose search pick is `search_olid`, with the work's editions available."""
    from lenny.core.bulk_import import matcher as m
    fake = (TargetedOL(work("Dune", ["Frank Herbert"], (search_olid, [], "eng")), editions=editions_of_dune()) if targeted
            else FakeOL(work("Dune", ["Frank Herbert"], (search_olid, [], "eng"))))
    row = dict(id=1, isbn=None, title="Dune", author="Frank Herbert", language="en", publisher=None, year=None, path=None)
    row.update(row_extra)
    with patch("lenny.core.bulk_import.matcher.search", fake), \
         patch("lenny.core.bulk_import.matcher.work_editions", return_value=editions or editions_of_dune()) as we:
        return dict(m.match_rows([row]))[1], (we, fake)


def test_the_best_edition_of_the_work_replaces_search_s_pick_using_the_files_year_and_publisher():
    out, (we, _) = refined(dict(year=2019, publisher="Ace Books"), targeted=True)
    assert out["state"] == "matched" and out["olid"] == 3 and out["match_year"] == 2019      # Ace + 2019, not the 1965 print search showed
    # and when the direct ask finds nothing, the scan (100 editions) picks the closest fit instead
    out, (we, _) = refined(dict(year=2019, publisher="Ace Books"))
    assert out["olid"] == 3 and we.call_args.kwargs["limit"] == 100


def test_the_year_alone_picks_the_right_edition_and_an_ebook_wins_a_tie():
    out, _ = refined(dict(year=2019))                                                         # the scan: eng e-book 2019 (5 is French: penalised)
    assert out["olid"] == 4
    out, _ = refined(dict(year=2019), targeted=True)                                          # the direct ask: an English 2019 edition
    assert out["olid"] in (3, 4)
    out, _ = refined(dict(year=1990))
    assert out["olid"] == 2


def test_without_year_or_publisher_there_is_nothing_to_refine_and_no_extra_request():
    out, (we, _) = refined(dict())
    assert out["olid"] == 1 and we.call_count == 0


def test_an_isbn_match_is_already_exact_and_never_refined(ol):
    from lenny.core.bulk_import import matcher as m
    row = dict(id=1, isbn="9780593099322", title=None, author=None, language=None, publisher="Ace", year=2019, path=None)
    with patch("lenny.core.bulk_import.matcher.work_editions") as we:
        out = dict(m.match_rows([row]))[1]
    assert out["olid"] == 102 and we.call_count == 0


def test_refinement_keeps_the_pick_when_lookup_fails_or_nothing_is_better():
    from lenny.core.bulk_import import matcher as m
    from lenny.core.bulk_import.matcher import UpstreamError
    fake = FakeOL(work("Dune", ["Frank Herbert"], (2, [], "eng")))
    row = dict(id=1, isbn=None, title="Dune", author="Frank Herbert", language="en", publisher=None, year=1990, path=None)
    for kwargs in (dict(side_effect=UpstreamError("down")), dict(return_value=[])):
        with patch("lenny.core.bulk_import.matcher.search", fake), patch("lenny.core.bulk_import.matcher.work_editions", **kwargs):
            out = dict(m.match_rows([row]))[1]
        assert out["state"] == "matched" and out["olid"] == 2 and "_work" not in out
    out, _ = refined(dict(year=1990), search_olid=2)                                           # already the best: unchanged
    assert out["olid"] == 2


def test_refined_twins_share_the_better_edition_and_get_their_own_copy():
    from lenny.core.bulk_import import matcher as m
    fake = FakeOL(work("Dune", ["Frank Herbert"], (1, [], "eng")))
    rows_ = [dict(id=i, isbn=None, title="Dune", author="Frank Herbert", language="en", publisher="Ace", year=2019, path=None) for i in (1, 2)]
    fake = TargetedOL(work("Dune", ["Frank Herbert"], (1, [], "eng")), editions=editions_of_dune())
    with patch("lenny.core.bulk_import.matcher.search", fake), patch("lenny.core.bulk_import.matcher.work_editions", return_value=editions_of_dune()) as we:
        out = dict(m.match_rows(rows_))
    asks = [c for c in fake.calls if "publish_year" in c.get("q", "")]
    assert out[1]["olid"] == out[2]["olid"] == 3 and out[1] is not out[2] and len(asks) == 1   # asked once for both


def test_a_tie_prefers_the_newer_record_not_the_old_print_edition():
    from lenny.core.bulk_import import matcher as m
    cands = [dict(olid=10, title="Dune", authors=["Frank Herbert"], isbns=[], language="eng", publisher=None, year=1965, work="w", isbn=None),
             dict(olid=20, title="Dune", authors=["Frank Herbert"], isbns=[], language="eng", publisher=None, year=2019, work="w", isbn=None)]
    ranked = m.rank(dict(title="Dune", author="Frank Herbert", language=None, publisher=None, year=None, path=None), cands)
    assert [c["olid"] for c in ranked] == [20, 10]


def test_year_closeness_is_graded():
    from lenny.core.bulk_import import matcher as m
    cand = lambda y: dict(year=y, language=None, publisher=None, format=None)
    row = dict(year=2019, language=None, publisher=None)
    assert [m._delta(row, cand(y)) for y in (2019, 2018, 2016, 2009, 1995, 1980)] == [4, 3, 1, 0, -2, -4]
    assert m._delta(row, dict(cand(2019), format="Ebook")) == 6 and m._delta(row, dict(cand(2019), format="Hardcover")) == 4


def test_an_old_edition_year_can_no_longer_win_a_close_call_but_never_hides_the_book():
    from lenny.core.bulk_import import matcher as m
    c = dict(olid=1, title="Dune", authors=["Frank Herbert"], isbns=[], language="eng", publisher=None, year=1920)
    assert m.score(dict(title="Dune", author="Frank Herbert", year=2019, language="en"), c) >= m.AUTO_MIN      # still the same book


def test_the_edition_with_the_files_publisher_and_year_is_asked_for_directly():
    out, (we, fake) = refined(dict(year=2019, publisher="Ace Books"), targeted=True)
    assert out["olid"] == 3 and out["match_year"] == 2019
    asked = [c["q"] for c in fake.calls if "publish_year" in c.get("q", "")]
    assert asked[0] == '(title:"Dune" AND author:"Frank Herbert") AND publisher:"Ace Books" AND publish_year:2019'
    assert we.call_count == 0                                                               # found directly: the scan never ran


def test_the_direct_asks_widen_from_publisher_and_year_to_year_to_publisher():
    out, (we, fake) = refined(dict(year=2019, publisher="No Such Press", language="en"), targeted=True)
    qs = [c["q"] for c in fake.calls if "publish_year" in c.get("q", "") or "publisher:" in c.get("q", "")]
    assert qs[0].endswith("publish_year:2019") and 'publisher:"No Such Press"' in qs[0]
    assert qs[1].endswith("publish_year:2019 AND language:eng")                              # then the year, with the language
    assert out["olid"] in (3, 4)                                                              # a 2019 English edition, not the 1965 print
    out, (we, fake) = refined(dict(publisher="Chilton"), targeted=True)
    assert out["olid"] == 1 and [c["q"] for c in fake.calls if "publisher:" in c.get("q", "")] == [
        '(title:"Dune" AND author:"Frank Herbert") AND publisher:"Chilton"']


def test_a_publisher_with_quotes_cannot_break_the_direct_question():
    out, (we, fake) = refined(dict(year=2019, publisher='Ace "Special" \\ Books'), targeted=True)
    q = [c["q"] for c in fake.calls if "publish_year" in c.get("q", "")][0]
    assert q.count('"') == 6 and "\\" not in q                                               # title, author, publisher: three quoted phrases


def test_when_nothing_matches_the_scan_still_finds_the_closest_year():
    out, (we, fake) = refined(dict(year=1991), targeted=True)                                 # no 1991 edition: 1990 is closest
    assert out["olid"] == 2 and we.call_count == 1
