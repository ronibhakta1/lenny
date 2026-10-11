"""Import every ready, staged row of a batch in the background.

The page used to loop over the rows itself, so closing the tab stopped the import
and a reload lost the progress bar. Now that each matched book's file is already on
the server (staging.py), importing is only a server-side copy plus a database row,
so the server can do all of it by itself: click Import, close the laptop, come back
to a finished list. Progress is in the batch summary (`counts.importing` / `imported`)
and in this job's counters, so a reload shows it too.

One import thread per process, separate from matching so neither waits for the
other. One active job per batch. Rows whose file has not reached the server yet are
not touched (`waiting`); they import when a later run starts, once their files arrive.
"""

import logging
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from lenny.core.bulk_import import batches, importer
from lenny.core.bulk_import.batches import (
    DUPLICATE, FAILED, IMPORTED, MATCHED, ImportRow, ImportRowBusy, ImportRowNotFound)
from lenny.core.db import session as db

logger = logging.getLogger(__name__)

CHUNK = 200
MAX_JOBS = 50

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="import-run")
_lock = threading.Lock()
_jobs: "OrderedDict[str, dict]" = OrderedDict()


def _ready(batch_id: int, retry_failed: bool):
    states = (MATCHED, FAILED) if retry_failed else (MATCHED,)
    return db.query(ImportRow).filter(
        ImportRow.batch_id == batch_id, ImportRow.skipped.is_(False), ImportRow.olid.is_not(None),
        ImportRow.state.in_(states))


def start(batch_id: int, retry_failed: bool = False) -> dict:
    """Queue the import. One active job per batch: asking again returns it."""
    batches.reconcile(batch_id)          # books deleted since they were matched as duplicates are importable again
    try:
        batches._get(batch_id)
        total = _ready(batch_id, retry_failed).filter(ImportRow.staged.is_(True)).count()
        waiting = _ready(batch_id, retry_failed).filter(ImportRow.staged.is_(False)).count()
    finally:
        db.rollback()
    with _lock:
        for job in _jobs.values():
            if job["batch_id"] == batch_id and job["status"] in ("queued", "running"):
                return dict(job)
        job = {"id": uuid.uuid4().hex, "batch_id": batch_id, "status": "queued", "total": total, "done": 0,
               "imported": 0, "duplicate": 0, "failed": 0, "waiting": waiting, "error": None}
        _jobs[job["id"]] = job
        while len(_jobs) > MAX_JOBS:
            _jobs.popitem(last=False)
        _executor.submit(_run, job, retry_failed)
        return dict(job)


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def latest(batch_id: int) -> dict | None:
    """The newest import job for the batch (this process only), for the batch summary."""
    with _lock:
        for job in reversed(_jobs.values()):
            if job["batch_id"] == batch_id:
                return dict(job)
    return None


def _run(job: dict, retry_failed: bool) -> None:
    job["status"] = "running"
    cursor, batch_id = 0, job["batch_id"]
    try:
        while True:
            ids = [i for (i,) in _ready(batch_id, retry_failed).filter(ImportRow.staged.is_(True), ImportRow.id > cursor)
                   .order_by(ImportRow.id).limit(CHUNK).with_entities(ImportRow.id).all()]
            db.rollback()
            if not ids:
                break
            for row_id in ids:
                try:
                    state = importer.import_staged(batch_id, row_id)["state"]
                except (ImportRowNotFound, ImportRowBusy, ValueError):
                    state = None            # removed, or being imported elsewhere, while we ran: leave it
                except batches.ImportBatchNotFound:
                    job["status"] = "done"  # the whole batch was cleared under us
                    return
                except Exception:
                    logger.exception("import of row %s failed", row_id)
                    state = FAILED
                job["done"] += 1
                if state == IMPORTED:
                    job["imported"] += 1
                elif state == DUPLICATE:
                    job["duplicate"] += 1
                elif state == FAILED:
                    job["failed"] += 1
            cursor = ids[-1]
        job["status"] = "done"
    except batches.ImportBatchExpired:
        job["status"] = "done"
    except Exception:
        logger.exception("import job failed")
        job["status"], job["error"] = "failed", "The import stopped unexpectedly. Start it again to continue."
    finally:
        db.remove()
