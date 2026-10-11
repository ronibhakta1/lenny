"""Background matching of a batch's rows.

One worker thread per process runs match jobs one after another, so matching a
big library never competes with readers for more than a thread, and Open Library
sees one paced client. Results are saved to the rows as they arrive, so the page
can poll the batch (or the job) and show partial progress, and a restart loses
nothing: unmatched rows are still `queued` and a new job picks them up.

Per process, on purpose: with several worker processes each runs its own job and
pacer, so Open Library may see up to that many times the paced rate. Keep one
worker for big imports. ponytail: a shared limiter needs a shared store; add it
only if multi-worker deployments do large imports.

Only rows the admin has not decided are touched: `decision == auto`, and the
write itself re-checks that, so a choice made while a job is running wins.
"""

import logging
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import bindparam

from lenny.core.bulk_import import batches, matcher
from lenny.core.bulk_import.batches import (
    AUTO, DUPLICATE, MATCHED, NEEDS_REVIEW, NO_MATCH, QUEUED, ImportRow, _now)
from lenny.core.db import session as db
from lenny.core.models import Item

logger = logging.getLogger(__name__)

TARGET_STATES = (QUEUED, NO_MATCH, NEEDS_REVIEW)  # "re-check" targets no_match (and review)
CHUNK = 500
FLUSH_EVERY = 25
MAX_JOBS = 50

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="import-match")
_lock = threading.Lock()
_jobs: "OrderedDict[str, dict]" = OrderedDict()


def _targets(batch_id: int, states, row_ids=None):
    q = db.query(ImportRow).filter(
        ImportRow.batch_id == batch_id, ImportRow.decision == AUTO, ImportRow.skipped.is_(False),
        ImportRow.state.in_(states))
    if row_ids is not None:
        q = q.filter(ImportRow.id.in_(row_ids))
    return q


def start(batch_id: int, state: str = QUEUED, row_ids: list | None = None) -> dict:
    """Queue a match job for the batch. One active job per batch: asking again
    while one runs returns that job (rows added meanwhile are picked up by it)."""
    if state not in TARGET_STATES:
        raise ValueError(f"'state' must be one of {', '.join(TARGET_STATES)}")
    if row_ids is not None:
        if (not isinstance(row_ids, list) or len(row_ids) > batches.MAX_ROWS_PER_CALL
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in row_ids)):
            raise ValueError(f"'row_ids' must be a list of at most {batches.MAX_ROWS_PER_CALL} row ids")
    try:
        batches._get(batch_id)
        total = _targets(batch_id, (state,), row_ids).count()
    finally:
        db.rollback()
    with _lock:
        for job in _jobs.values():
            if job["batch_id"] == batch_id and job["status"] in ("queued", "running"):
                return dict(job)
        job = {"id": uuid.uuid4().hex, "batch_id": batch_id, "status": "queued",
               "total": total, "done": 0, "error": None, "retry_after": None}
        _jobs[job["id"]] = job
        while len(_jobs) > MAX_JOBS:
            _jobs.popitem(last=False)
        _executor.submit(_run, job, state, row_ids)
        return dict(job)


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def latest(batch_id: int) -> dict | None:
    """The newest match job for the batch (this process only), so the page can show
    "rate limited, retry in N s" instead of rows that stay queued with no reason."""
    with _lock:
        for job in reversed(_jobs.values()):
            if job["batch_id"] == batch_id:
                return dict(job)
    return None


_SAVED = ("state", "olid", "auto_picked", "candidates_count", "confidence", "match_title", "match_author", "error")


def _save(batch_id: int, results: list, state: str) -> None:
    """Write answers in one executemany; rows decided or moved on in the meantime
    (decision no longer auto, or no longer in `state`) are left alone."""
    olids = [u["olid"] for _, u in results if u.get("state") == MATCHED]
    have = {o for (o,) in db.query(Item.openlibrary_edition).filter(
        Item.openlibrary_edition.in_(olids)).all()} if olids else set()
    params = []
    for row_id, update in results:
        if update.get("olid") in have:
            update = dict(update, state=DUPLICATE)
        # Every row sets every column, so one statement serves them all.
        full = {"auto_picked": False, "candidates_count": 0, "confidence": 0, "olid": None,
                "match_title": None, "match_author": None, "error": None}
        full.update(update)
        params.append({"b_id": row_id, **{f"v_{k}": full[k] for k in _SAVED}})
    if params:
        stmt = (ImportRow.__table__.update()
                .where(ImportRow.id == bindparam("b_id"), ImportRow.batch_id == batch_id,
                       ImportRow.decision == AUTO, ImportRow.state == state)
                .values({k: bindparam(f"v_{k}") for k in _SAVED}))
        db.execute(stmt, params)
    now = _now()
    db.query(batches.ImportBatch).filter(batches.ImportBatch.id == batch_id).update(
        {"updated_at": now, "expires_at": now + batches.RETENTION}, synchronize_session=False)
    db.commit()


def _run(job: dict, state: str, row_ids) -> None:
    job["status"] = "running"
    cursor = 0
    cols = ("id", "isbn", "title", "author", "language", "publisher", "year", "path")
    try:
        while True:
            chunk = [dict(zip(cols, r)) for r in
                     _targets(job["batch_id"], (state,), row_ids).filter(ImportRow.id > cursor)
                     .order_by(ImportRow.id).limit(CHUNK)
                     .with_entities(*(getattr(ImportRow, c) for c in cols)).all()]
            db.rollback()
            if not chunk:
                break
            buffer = []
            for item in matcher.match_rows(chunk, fresh=state != QUEUED):
                buffer.append(item)
                job["done"] += 1
                if len(buffer) >= FLUSH_EVERY:
                    _save(job["batch_id"], buffer, state)
                    buffer = []
            if buffer:
                _save(job["batch_id"], buffer, state)
            cursor = chunk[-1]["id"]
        job["status"] = "done"
    except matcher.UpstreamError as e:
        job["status"], job["error"], job["retry_after"] = "failed", str(e), e.retry_after
    except Exception:
        logger.exception("import match job failed")
        job["status"], job["error"] = "failed", "Matching stopped unexpectedly."
    finally:
        db.remove()
