"""Load a picked set of free books, one at a time, in the background."""

import logging
import threading
from datetime import datetime, timedelta, timezone

from lenny.core.db import session as db
from lenny.core.imports import DOWNLOADING, PENDING, ImportJob
from lenny.core.models import Item
from lenny.core.preload import catalog
from lenny.core.standardebooks import SOURCE, import_one

logger = logging.getLogger(__name__)

MAX_PICK = 200
_running = threading.Lock()  # one load per process; the route also guards across workers


def existing_among(olids) -> set:
    """Which of these editions are already in the library."""
    olids = list(olids)
    if not olids:
        return set()
    try:
        return {o for (o,) in db.query(Item.openlibrary_edition).filter(
            Item.openlibrary_edition.in_(olids)).all()}
    finally:
        db.rollback()


ACTIVE_WITHIN = timedelta(minutes=30)


def running() -> bool:
    """True while books of this source are still queued or downloading. Reads the
    shared progress records, so it holds across worker processes; a crashed run
    stops counting after ACTIVE_WITHIN."""
    cutoff = datetime.now(timezone.utc) - ACTIVE_WITHIN
    for r in ImportJob.list():
        if r["source"] == SOURCE and r["status"] in (PENDING, DOWNLOADING) and r.get("updated_at"):
            at = datetime.fromisoformat(r["updated_at"])
            if (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) > cutoff:  # SQLite hands back naive
                return True
    return False


def plan(olids=(), source_ids=(), collection=None) -> dict:
    """What a pick would do, without doing it."""
    if len(olids) + len(source_ids) > MAX_PICK:
        raise ValueError(f"Pick at most {MAX_PICK} books at a time (use a collection for more).")
    books, unknown = catalog.find(olids, source_ids, collection)
    have = existing_among(b["olid"] for b in books)
    return {"to_load": [b for b in books if b["olid"] not in have],
            "already_in_library": len(have), "unknown": unknown}


def load(books: list) -> dict:
    """Load each book, continuing past failures. Returns counts."""
    counts = {"uploaded": 0, "failed": 0, "not_in_set": 0}
    if not _running.acquire(blocking=False):
        return counts
    try:
        for b in books:
            ImportJob.record(SOURCE, b["olid"], PENDING, title=b["title"])
        for b in books:
            outcome = import_one(b["olid"], b["source_id"], title=b["title"])
            counts[outcome] += 1
    finally:
        _running.release()
        db.remove()
    return counts
