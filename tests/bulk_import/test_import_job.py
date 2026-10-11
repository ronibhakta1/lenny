"""Importing a whole batch on the server (the import job): the page starts it and may close."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from tests.bulk_import.test_batches import (  # noqa: F401  (fixtures and helpers shared)
    API, admin_headers, app_module, clean_tables, client, new_batch, pg, rows, set_state)
from tests.bulk_import.test_match import db_items, staged_s3  # noqa: F401


@pytest.fixture(autouse=True)
def no_leftover_jobs():
    from lenny.core.bulk_import import import_job
    import_job._jobs.clear()
    yield
    import_job._jobs.clear()


def new_job(batch_id, retry_failed=False):
    return {"id": "t", "batch_id": batch_id, "status": "queued", "total": 0, "done": 0, "imported": 0,
            "duplicate": 0, "failed": 0, "waiting": 0, "error": None}


def run(batch_id, retry_failed=False):
    from lenny.core.bulk_import import import_job
    job = new_job(batch_id)
    import_job._run(job, retry_failed)
    return job


def staged_batch(store, n, olid_base=100, unstaged=0):
    """A batch of n matched rows whose files are on the server, plus `unstaged` matched rows whose are not."""
    from lenny.core.bulk_import import batches as b, staging
    bid = new_batch(); b.upsert_rows(bid, rows(n + unstaged))
    ids = [r["id"] for r in b.list_rows(bid, limit=200)["rows"]] if n + unstaged <= 200 else None
    set_state(bid, **{str(i): dict(state="matched", olid=olid_base + i, staged=(k < n)) for k, i in enumerate(ids)})
    for k, i in enumerate(ids[:n]):
        store.objects[staging.key(bid, i)] = b"epub"
    return bid, ids


def test_the_job_imports_every_staged_ready_row_and_leaves_the_rest(staged_s3, db_items):
    from lenny.core.bulk_import import batches as b, import_job, staging
    from lenny.core.models import Item
    bid, ids = staged_batch(staged_s3, 4, unstaged=2)
    set_state(bid, **{str(ids[0]): dict(skipped=True)})                      # skipped: never imported
    set_state(bid, **{str(ids[1]): dict(state="needs_review", olid=None)})   # not matched: not ready
    job = run(bid)
    assert job["status"] == "done" and job["done"] == 2 and job["imported"] == 2
    got = {r["id"]: r for r in b.list_rows(bid)["rows"]}
    assert [got[i]["state"] for i in ids] == ["matched", "needs_review", "imported", "imported", "matched", "matched"]
    assert db_items.query(Item).count() == 2
    assert [k for k in staged_s3.objects if k.startswith("import-staging/")] == [staging.key(bid, ids[0]), staging.key(bid, ids[1])]


def test_start_reports_how_many_rows_are_still_waiting_for_their_files(staged_s3, db_items):
    from lenny.core.bulk_import import import_job
    bid, ids = staged_batch(staged_s3, 3, unstaged=2)
    with patch.object(import_job, "_executor", MagicMock()):
        job = import_job.start(bid)
    assert (job["total"], job["waiting"], job["status"]) == (3, 2, "queued")


def test_duplicates_and_failures_are_counted_and_a_failure_does_not_stop_the_rest(staged_s3, db_items):
    from lenny.core.bulk_import import batches as b
    from lenny.core.models import FormatEnum, Item
    db_items.add(Item(openlibrary_edition=102, formats=FormatEnum.EPUB)); db_items.commit()
    bid, ids = staged_batch(staged_s3, 4)                                    # olids 101..104 (ids start at 1)
    real = staged_s3.copy_object.side_effect
    def flaky(Bucket, CopySource, Key):
        if Key == f"{100 + ids[3]}.epub":
            raise RuntimeError("garage hiccup")
        return real(Bucket, CopySource, Key)
    staged_s3.copy_object.side_effect = flaky
    job = run(bid)
    assert (job["imported"], job["duplicate"], job["failed"], job["done"]) == (2, 1, 1, 4)
    states = {r["id"]: r["state"] for r in b.list_rows(bid)["rows"]}
    assert states[ids[1]] == "duplicate" and states[ids[3]] == "failed"


def test_failed_rows_are_retried_only_when_asked(staged_s3, db_items):
    from lenny.core.bulk_import import batches as b
    bid, ids = staged_batch(staged_s3, 2)
    set_state(bid, **{str(ids[0]): dict(state="failed", error="x", staged=True)})
    assert run(bid)["done"] == 1                                             # only the matched one
    assert b.list_rows(bid)["rows"][0]["state"] == "failed"
    job = run(bid, retry_failed=True)
    assert job["imported"] == 1 and b.list_rows(bid)["rows"][0]["state"] == "imported"


def test_one_job_per_batch_and_each_batch_gets_its_own(staged_s3, db_items):
    from lenny.core.bulk_import import import_job
    a, _ = staged_batch(staged_s3, 1)
    c, _ = staged_batch(staged_s3, 1, olid_base=500)
    with patch.object(import_job, "_executor", MagicMock()) as ex:
        first, again, other = import_job.start(a), import_job.start(a), import_job.start(c)
        assert first["id"] == again["id"] != other["id"] and ex.submit.call_count == 2
        import_job._jobs[first["id"]]["status"] = "done"
        assert import_job.start(a)["id"] != first["id"]
    assert import_job.latest(a)["batch_id"] == a and import_job.get("nope") is None and import_job.latest(99999) is None


def test_a_row_removed_while_the_job_runs_is_skipped_quietly(staged_s3, db_items):
    from lenny.core.bulk_import import batches as b, importer
    bid, ids = staged_batch(staged_s3, 3)
    real = importer.import_staged
    def remove_next_then_import(batch_id, row_id):
        if row_id == ids[0]:
            b.delete_row(bid, ids[1])
        return real(batch_id, row_id)
    with patch.object(importer, "import_staged", remove_next_then_import):
        job = run(bid)
    assert job["status"] == "done" and job["imported"] == 2


def test_a_batch_cleared_while_the_job_runs_ends_it_cleanly(staged_s3, db_items):
    from lenny.core.bulk_import import batches as b, importer
    bid, ids = staged_batch(staged_s3, 3)
    real = importer.import_staged
    def clear_then_import(batch_id, row_id):
        b.delete_batch(bid)
        return real(batch_id, row_id)
    with patch.object(importer, "import_staged", clear_then_import):
        job = run(bid)
    assert job["status"] == "done"


def test_an_unexpected_error_fails_the_job_without_leaking_details():
    bid = new_batch()
    with patch("lenny.core.bulk_import.import_job._ready", side_effect=RuntimeError("secret internals")):
        job = run(bid)
    assert job["status"] == "failed" and "secret" not in job["error"] and "Start it again" in job["error"]


def test_a_big_batch_is_processed_in_chunks(staged_s3, db_items):
    from lenny.core.bulk_import import batches as b, import_job
    bid = new_batch()
    n = import_job.CHUNK * 2 + 17
    for i in range(0, n, 500):
        b.upsert_rows(bid, rows(min(500, n - i), start=i))
    ids = [r["id"] for r in b.list_rows(bid, limit=200)["rows"]]
    from lenny.core.db import session
    from lenny.core.bulk_import.batches import ImportRow
    session.query(ImportRow).filter(ImportRow.batch_id == bid).update({"state": "matched", "olid": 7, "staged": True})
    session.commit()
    with patch("lenny.core.bulk_import.importer.import_staged", return_value={"state": "imported"}) as imp:
        job = run(bid)
    assert job["done"] == n and job["imported"] == n and imp.call_count == n


# ─── routes ──────────────────────────────────────────────────────────────────

def test_import_job_over_http(client, staged_s3, db_items):
    from lenny.core.bulk_import import import_job
    class Inline:
        def submit(self, fn, *a): fn(*a)
    bid = client.post(API).json()["id"]
    client.put(f"{API}/{bid}/rows", json={"rows": [dict(path=f"{i}.epub", size=i + 1, mtime=1) for i in range(3)]})
    ids = [r["id"] for r in client.get(f"{API}/{bid}/rows").json()["rows"]]
    from lenny.core.bulk_import import staging
    set_state(bid, **{str(i): dict(state="matched", olid=300 + k, staged=(k < 2)) for k, i in enumerate(ids)})
    for i in ids[:2]:
        staged_s3.objects[staging.key(bid, i)] = b"epub"
    assert client.get(f"{API}/{bid}").json()["import"] is None
    with patch.object(import_job, "_executor", Inline()):
        r = client.post(f"{API}/{bid}/import")
    assert r.status_code == 202
    job = r.json()
    assert (job["total"], job["waiting"]) == (2, 1) and set(job) >= {"id", "status", "done", "imported", "duplicate", "failed", "error"}
    summary = client.get(f"{API}/{bid}").json()
    assert summary["counts"] == {"imported": 2, "matched": 1}
    assert summary["import"]["status"] == "done" and summary["import"]["imported"] == 2
    assert client.get(f"/v1/api/admin/import/jobs/{job['id']}").json()["imported"] == 2
    assert client.get("/v1/api/admin/import/jobs/unknown").status_code == 404
    assert client.post(f"{API}/{bid}/import", json={"retry_failed": "yes"}).status_code == 400
    assert client.post(f"{API}/{bid + 99}/import").status_code == 404


def test_the_import_routes_require_admin(app_module):
    from fastapi.testclient import TestClient
    c = TestClient(app_module)
    assert c.post(f"{API}/1/import", json={}).status_code == 403
    assert c.get("/v1/api/admin/import/jobs/x").status_code == 403


def test_two_batches_importing_one_edition_at_once_import_it_once(pg, staged_s3):
    from lenny.core.bulk_import import batches as b, staging
    from lenny.core.db import session
    from lenny.core.models import Item
    made = []
    for _ in range(4):
        bid = b.create_batch()["id"]; b.upsert_rows(bid, rows(1, title="Dune"))
        rid = b.list_rows(bid)["rows"][0]["id"]
        set_state(bid, **{str(rid): dict(state="matched", olid=101, staged=True)})
        staged_s3.objects[staging.key(bid, rid)] = b"epub"
        made.append(bid)
    session.remove()
    results, barrier = [], threading.Barrier(len(made))
    def worker(bid):
        try:
            barrier.wait()
            results.append(run(bid))
        finally:
            session.remove()
    ts = [threading.Thread(target=worker, args=(m,)) for m in made]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(j["imported"] for j in results) == 1 and sum(j["duplicate"] for j in results) == 3
    assert session.query(Item).filter(Item.openlibrary_edition == 101).count() == 1
