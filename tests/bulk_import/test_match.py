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

    def __call__(self, params, retries=3):
        self.calls.append(dict(params))
        if "q" in params and params["q"].startswith("isbn:"):
            wanted = set(params["q"].replace("isbn:", "").strip("()").replace(" OR ", " ").split())
            return [w for w in self.works if any(set(e["isbn"]) & wanted for e in w["editions"]["docs"])]
        t, a = params.get("title", "").lower(), params.get("author", "").lower()
        found = [w for w in self.works if t and t.split(":")[0] in w["title"].lower()]
        # Like Open Library: an author term that matches no author empties the result.
        return [w for w in found if not a or any(a in n.lower() for n in w["author_name"])]


@pytest.fixture
def ol():
    fake = FakeOL(
        work("Dune", ["Frank Herbert"], (101, ["9780441013593", "0441013597"], "eng"), (102, ["9780593099322"], "fre")),
        work("The Left Hand of Darkness", ["Ursula K. Le Guin"], (201, ["9780441478125"], "eng")),
    )
    with patch("lenny.core.bulk_import.matcher.search", fake):
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
    with patch("lenny.core.bulk_import.matcher.search", fake):
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
    gen = m.match_rows(rl)
    first = next(gen)
    assert first[0] == 1 and len(ol.calls) == 1          # lazily: second not asked yet
    assert dict(gen)[2]["olid"] == 201


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


def test_retry_after_is_capped_and_persistent_429_gives_up(fresh_guard):
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", return_value=resp(429, headers={"Retry-After": "99999"})) as get, \
         patch("lenny.core.bulk_import.matcher.time.sleep") as sleep:
        with pytest.raises(m.UpstreamError, match="rate limiting"):
            m.search({"title": "x"})
    assert get.call_count == m.RETRIES + 1
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


def test_requests_identify_the_server_and_log_in_when_configured(fresh_guard):
    from lenny import configs
    from lenny.core.bulk_import import matcher as m
    with patch("requests.get", return_value=resp()) as get, \
         patch.object(configs, "OL_S3_ACCESS_KEY", "ak"), patch.object(configs, "OL_S3_SECRET_KEY", "sk"), \
         patch.dict(configs.LENNY_HTTP_HEADERS, {"User-Agent": "LennyImportBot/1.0 (me@example.org)"}):
        m.search({"title": "x"})
    headers = get.call_args.kwargs["headers"]
    assert "me@example.org" in headers["User-Agent"] and headers["Authorization"] == "LOW ak:sk"


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
    assert anon["ol_requests_per_second"] == 1.0 and anon["ol_identified"] is False
    assert anon["formats"] == ["epub"] and anon["retention_days"] == 30 and anon["isbn_batch"] == 50
    assert anon["max_file_mb"] == 100 and anon["max_rows_per_call"] == 500
    with patch.object(configs, "CONTACT_EMAIL", "me@example.org"):
        assert limits.limits()["ol_requests_per_second"] == 3.0


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
    def search_then_admin_picks(params, retries=3):
        out = real(params, retries)
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


def test_an_unexpected_error_does_not_leak_details():
    bid = batch_with(dict(title="Dune"))
    with patch("lenny.core.bulk_import.matcher.search", side_effect=RuntimeError("secret internals")):
        job = run_job(bid)
    assert job["status"] == "failed" and "secret" not in job["error"]


def test_partial_results_are_saved_as_the_job_goes(ol):
    from lenny.core.bulk_import import batches as b, jobs
    bid = batch_with(*[dict(title="Dune", author="Frank Herbert") for _ in range(jobs.FLUSH_EVERY + 5)])
    seen = []
    real = ol.__call__
    def spying(params, retries=3):
        seen.append(b.get_summary(bid)["counts"].get("matched", 0))
        return real(params, retries)
    with patch("lenny.core.bulk_import.matcher.search", spying):
        run_job(bid)
    assert max(seen) >= jobs.FLUSH_EVERY and b.get_summary(bid)["counts"]["matched"] == jobs.FLUSH_EVERY + 5


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
    assert set(cands[0]) == {"olid", "title", "authors", "year", "publisher", "language", "isbn", "score"}
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
