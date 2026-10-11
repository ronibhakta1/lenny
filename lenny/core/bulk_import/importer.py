"""Import one matched row: store its EPUB and create the library item.

The page uploads files one at a time (a few in flight at once); each call here
claims its row, hands the file to LennyAPI.add, and records the outcome on the
row. The server bounds how many run together (routes use the upload limiter),
whatever the page does. Metadata the row only needed for matching is dropped
once the book is in.

The outcome is on the returned row's `state`, not the HTTP status: `imported`,
`duplicate` (already in the library, which is fine), `not_epub`, or `failed`.
A row left `importing` by a crash is simply claimed again by the next attempt.
"""

import logging

from lenny.core.bulk_import import batches
from lenny.core.bulk_import.batches import (
    FAILED, IMPORTED, IMPORTING, MATCHED, NOT_EPUB, DUPLICATE, ImportRow, ImportRowNotFound, _row_dict, _touch)
from lenny.core.db import session as db
from lenny.core.exceptions import FileTooLargeError, InvalidFileError, ItemExistsError, LennyAPIError
from lenny.core.models import _xact_lock

logger = logging.getLogger(__name__)

_CLAIMABLE = (MATCHED, FAILED, IMPORTING)
_DROP = dict.fromkeys(("isbn", "title", "author", "language", "publisher", "year"))


def _claim(batch_id: int, row_id: int):
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = batches._get(batch_id)
        row = db.query(ImportRow).filter(
            ImportRow.batch_id == batch_id, ImportRow.id == row_id).one_or_none()
        if row is None:
            raise ImportRowNotFound(f"Row {row_id} not found.")
        if row.state == IMPORTED:
            return None, _row_dict(row)
        if row.state not in _CLAIMABLE or not row.olid or row.skipped:
            raise ValueError("Only a matched row that is not skipped can be imported.")
        row.state, row.error = IMPORTING, None
        _touch(batch)
        olid, encrypted = row.olid, row.encrypted
        db.commit()
        return (olid, encrypted), None
    finally:
        db.rollback()


def _finish(row_id: int, **fields) -> dict:
    try:
        db.query(ImportRow).filter(ImportRow.id == row_id).update(fields, synchronize_session=False)
        db.commit()
        return _row_dict(db.query(ImportRow).filter(ImportRow.id == row_id).one())
    finally:
        db.rollback()


def import_row(batch_id: int, row_id: int, upload) -> dict:
    from lenny.core.api import LennyAPI  # lazy: api imports a lot
    claim, done = _claim(batch_id, row_id)
    if done:
        return done  # already imported: retries are harmless
    olid, encrypted = claim
    try:
        LennyAPI.add(olid, [upload], encrypt=encrypted)
    except ItemExistsError:
        return _finish(row_id, state=DUPLICATE, error=None)
    except InvalidFileError as e:
        return _finish(row_id, state=NOT_EPUB, error=str(e)[:200])
    except FileTooLargeError as e:
        return _finish(row_id, state=FAILED, error=str(e)[:200])
    except LennyAPIError as e:
        logger.warning("import of row %s failed: %s", row_id, e)
        return _finish(row_id, state=FAILED, error="Could not store this book; try again.")
    except Exception:
        logger.exception("import of row %s failed", row_id)
        return _finish(row_id, state=FAILED, error="Could not store this book; try again.")
    return _finish(row_id, state=IMPORTED, error=None, **_DROP)
