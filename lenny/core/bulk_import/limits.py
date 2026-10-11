"""What this server will do for a bulk import, in one place.

The page reads these numbers (`GET /admin/import/limits`) instead of hard-coding
them, and the matcher paces itself by them. Open Library allows 1 request a
second to anonymous clients and 3 to identified ones (a User-Agent with contact
details; a logged-in account may be treated better still, which is unverified).
"""

import threading
import time

from lenny.core.bulk_import import batches

ISBN_BATCH = 50        # ISBNs per Open Library query
ANON_RPS = 1.0
IDENTIFIED_RPS = 3.0
MAX_OLIDS_PER_CHECK = 1000


def identified() -> bool:
    from lenny import configs
    # Only the documented route counts; a login is sent too but its effect on the limit is unverified.
    return bool(configs.CONTACT_EMAIL)


def ol_rps() -> float:
    return IDENTIFIED_RPS if identified() else ANON_RPS


class Pacer:
    """Spaces calls at least 1/rps apart across all threads. Reads the rate each
    time, so changing credentials takes effect without a restart."""

    def __init__(self, rps=ol_rps, clock=time.monotonic, sleep=time.sleep):
        self._rps, self._clock, self._sleep = rps, clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + 1.0 / self._rps()
        if start > now:
            self._sleep(start - now)


def limits() -> dict:
    from lenny import configs
    from lenny.core.api import LennyAPI
    rps = ol_rps()
    return {
        "max_rows_per_call": batches.MAX_ROWS_PER_CALL,
        "isbn_batch": ISBN_BATCH,
        "ol_requests_per_second": rps,
        "ol_identified": identified(),
        "ol_logged_in": bool(configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY),
        "max_file_mb": LennyAPI.MAX_FILE_SIZE // (1024 * 1024),
        "upload_concurrency": configs.UPLOAD_CONCURRENCY,
        "retention_days": batches.RETENTION.days,
        "formats": ["epub"],
    }
