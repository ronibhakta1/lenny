"""Keep a book's file on the server before it is imported.

The page cannot hold files for long: a reload, another app on a phone or a
discarded background tab all lose them, and a browser will not reopen a file by
itself. So the page sends each matched book's file here as soon as it is matched,
and "Import" later only finishes the job on the server (a copy inside the object
store plus the database row). Nothing then has to stay connected to the browser.

A staged copy lives under `import-staging/{batch}/{row}.epub` in the bookshelf
bucket (no real book key starts that way) and is deleted with its row, its batch,
or when the batch expires.
"""

import logging

from lenny.core import s3
from lenny.core.bulk_import import batches
from lenny.core.bulk_import.batches import (
    FAILED, MATCHED, ImportRow, ImportRowBusy, ImportRowNotFound, _row_dict, _touch)
from lenny.core.db import session as db
from lenny.core.exceptions import S3UploadError
from lenny.core.models import _xact_lock

logger = logging.getLogger(__name__)

PREFIX = "import-staging"
STAGEABLE = (MATCHED, FAILED)


def key(batch_id: int, row_id: int) -> str:
    return f"{PREFIX}/{batch_id}/{row_id}.epub"


def stage(batch_id: int, row_id: int, upload) -> dict:
    """Store `upload` as the row's staged file (replacing an earlier one) and
    return the row. The file is checked like any import: EPUB only, size, content."""
    from lenny.core.api import LennyAPI  # lazy: api imports a lot
    try:
        batches._get(batch_id)
        row = db.query(ImportRow).filter(
            ImportRow.batch_id == batch_id, ImportRow.id == row_id).one_or_none()
        if row is None:
            raise ImportRowNotFound(f"Row {row_id} not found.")
        if row.state not in STAGEABLE or row.skipped:
            raise ValueError("Only a matched row that is not skipped can be uploaded.")
    finally:
        db.rollback()
    LennyAPI.validate_files([upload])           # InvalidFileError / FileTooLargeError, before any S3 write
    upload.file.seek(0)
    object_key = key(batch_id, row_id)
    try:                                         # outside any transaction: this can take a while
        s3.upload_fileobj(upload.file, s3.BOOKSHELF_BUCKET, object_key,
                          ExtraArgs={"ContentType": "application/epub+zip"})
    except Exception as e:
        logger.error("Could not stage row %s: %s", row_id, e)
        raise S3UploadError("Could not store the uploaded file.") from e
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = batches._get(batch_id)
        row = db.query(ImportRow).filter(
            ImportRow.batch_id == batch_id, ImportRow.id == row_id).one_or_none()
        if row is None:                          # removed while the file was uploading
            drop(rows=[(batch_id, row_id)])
            raise ImportRowNotFound(f"Row {row_id} not found.")
        row.staged = True
        _touch(batch)
        db.commit()
        return _row_dict(row)
    finally:
        db.rollback()


def drop(batches=(), rows=()) -> None:
    """Delete staged objects: every one of the given batches, and the given (batch, row) pairs."""
    keys = [key(b, r) for b, r in rows]
    for b in batches:
        try:
            keys += list(s3.get_keys(prefix=f"{PREFIX}/{b}/"))
        except Exception as e:
            logger.warning("Could not list staged files of batch %s: %s", b, e)
    for k in keys:
        try:
            s3.delete_object(Bucket=s3.BOOKSHELF_BUCKET, Key=k)
        except Exception as e:
            logger.warning("Could not delete staged file %s: %s", k, e)
