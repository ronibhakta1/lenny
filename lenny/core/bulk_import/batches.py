#!/usr/bin/env python

"""
    Import batches: server-side state for a bulk EPUB import.

    An admin drops thousands of EPUBs on one page. Each file becomes a row here
    so a closed tab, a dropped connection or a crash loses nothing: the page
    reloads the batch from the API and carries on.

    Only what cannot be recomputed is kept. A row is the file's identity (path,
    size, modified time), the inputs the matcher needs (ISBN, title, author),
    its outcome (state, edition, decision) and a short error. Candidate lists
    are fetched when a row is opened, and an imported row drops its metadata.

    A batch is kept for 30 days from its last write (reads never extend it, or
    a polling page would keep it alive forever). Expired batches are purged
    lazily when batches are created or listed, so a small machine needs no
    scheduler.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import hashlib
import logging
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer,
    SmallInteger, String, UniqueConstraint, and_, case, func, or_,
)

from lenny.core.db import Base, session as db
from lenny.core.exceptions import LennyAPIError
from lenny.core.models import _xact_lock

logger = logging.getLogger(__name__)

RETENTION = timedelta(days=30)
MAX_ROWS_PER_CALL = 500
MAX_PAGE = 200
MAX_BATCHES_LISTED = 20

QUEUED, MATCHED, NEEDS_REVIEW, NO_MATCH = "queued", "matched", "needs_review", "no_match"
DUPLICATE, NOT_EPUB, IMPORTING, IMPORTED, FAILED = "duplicate", "not_epub", "importing", "imported", "failed"
STATES = (QUEUED, MATCHED, NEEDS_REVIEW, NO_MATCH, DUPLICATE, NOT_EPUB, IMPORTING, IMPORTED, FAILED)
# States whose metadata a re-sent row may still refresh.
_REFRESHABLE = (QUEUED, NO_MATCH, NEEDS_REVIEW, FAILED)
# States a bulk skip/unskip leaves alone: there is nothing left to decide.
_SETTLED = (IMPORTING, IMPORTED, NOT_EPUB)

AUTO, USER = "auto", "user"

# Column limits, enforced here so a hostile or buggy client cannot fill the table.
_MAX = {"path": 1024, "title": 300, "author": 200, "publisher": 200, "language": 16, "error": 200}


class ImportBatchNotFound(LennyAPIError): pass
class ImportBatchExpired(LennyAPIError): pass
class ImportRowNotFound(LennyAPIError): pass
class ImportRowBusy(LennyAPIError): pass


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (Index("idx_import_batches_expires", "expires_at"),)

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)


class ImportRow(Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_key", name="uq_import_rows_batch_key"),
        Index("idx_import_rows_batch_state", "batch_id", "state", "id"),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    batch_id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False)
    # sha1(path | size | mtime): what lets a re-dropped folder re-attach to its rows.
    row_key = Column(String(40), nullable=False)
    path = Column(String(_MAX["path"]), nullable=False)
    size = Column(BigInteger, nullable=False)
    mtime = Column(BigInteger, nullable=False)

    state = Column(String(16), nullable=False, default=QUEUED)
    decision = Column(String(8), nullable=False, default=AUTO)
    skipped = Column(Boolean, nullable=False, default=False)
    encrypted = Column(Boolean, nullable=False, default=False)
    olid = Column(BigInteger, nullable=True)
    auto_picked = Column(Boolean, nullable=False, default=False)
    candidates_count = Column(Integer, nullable=False, default=0)
    confidence = Column(SmallInteger, nullable=False, default=0)  # 0-100

    # The matched Open Library record, kept after import so the list can still
    # name the book once the EPUB's own metadata is dropped.
    match_title = Column(String(_MAX["title"]), nullable=True)
    match_author = Column(String(_MAX["author"]), nullable=True)

    isbn = Column(String(13), nullable=True)
    title = Column(String(_MAX["title"]), nullable=True)
    author = Column(String(_MAX["author"]), nullable=True)
    language = Column(String(_MAX["language"]), nullable=True)
    publisher = Column(String(_MAX["publisher"]), nullable=True)
    year = Column(SmallInteger, nullable=True)
    error = Column(String(_MAX["error"]), nullable=True)
    # When an import of this row started; an `importing` row older than the
    # claim timeout is taken to be abandoned (crash, closed tab) and may be retried.
    claimed_at = Column(DateTime(timezone=True), nullable=True)


_ROW_FIELDS = (
    "id", "path", "size", "mtime", "state", "decision", "skipped", "encrypted", "olid",
    "auto_picked", "candidates_count", "confidence", "match_title", "match_author", "isbn", "title", "author",
    "language", "publisher", "year", "error",
)
_METADATA = ("isbn", "title", "author", "language", "publisher", "year")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres aware ones. Treat naive as UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def row_key(path: str, size: int, mtime: int) -> str:
    return hashlib.sha1(f"{path}\0{size}\0{mtime}".encode("utf-8")).hexdigest()


def _clean_isbn(value) -> str | None:
    digits = "".join(c for c in str(value or "") if c.isdigit() or c in "xX").upper()
    return digits if len(digits) in (10, 13) else None


def _clean_row(raw: dict, index: int) -> dict:
    """Validate and normalise one client row; raises ValueError naming the row."""
    if not isinstance(raw, dict):
        raise ValueError(f"row {index}: must be an object")
    path, size, mtime = raw.get("path"), raw.get("size"), raw.get("mtime")
    if isinstance(path, str):
        # macOS hands out decomposed (NFD) names, Windows and Linux composed (NFC);
        # one form keeps a folder re-dropped from another machine attaching to its rows.
        path = unicodedata.normalize("NFC", path).replace("\\", "/")
    if not isinstance(path, str) or not 0 < len(path) <= _MAX["path"]:
        raise ValueError(f"row {index}: 'path' must be a string of 1-{_MAX['path']} characters")
    for name, value in (("size", size), ("mtime", mtime)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"row {index}: '{name}' must be a non-negative integer")
    state = raw.get("state")
    if state not in (None, NOT_EPUB):
        raise ValueError(f"row {index}: 'state' may only be '{NOT_EPUB}'")
    year = raw.get("year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 0 < year < 10000):
        year = None
    clean = {
        "row_key": row_key(path, size, mtime), "path": path, "size": size, "mtime": mtime,
        "state": state or QUEUED, "isbn": _clean_isbn(raw.get("isbn")), "year": year,
    }
    for name in ("title", "author", "language", "publisher"):
        value = raw.get(name)
        clean[name] = str(value).strip()[:_MAX[name]] or None if value is not None else None
    return clean


# ─── batches ────────────────────────────────────────────────────────────────

def purge_expired() -> int:
    """Delete expired batches and their rows. Returns how many batches went."""
    ids = [i for (i,) in db.query(ImportBatch.id).filter(ImportBatch.expires_at < _now()).all()]
    if ids:
        # Rows first: SQLite does not enforce ON DELETE CASCADE.
        db.query(ImportRow).filter(ImportRow.batch_id.in_(ids)).delete(synchronize_session=False)
        db.query(ImportBatch).filter(ImportBatch.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return len(ids)


def _get(batch_id: int) -> ImportBatch:
    batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).one_or_none()
    if batch is None:
        raise ImportBatchNotFound(f"Batch {batch_id} not found.")
    if _aware(batch.expires_at) < _now():
        raise ImportBatchExpired(f"Batch {batch_id} has expired.")
    return batch


def _touch(batch: ImportBatch) -> None:
    now = _now()
    batch.updated_at, batch.expires_at = now, now + RETENTION


def summary(batch: ImportBatch) -> dict:
    counts = dict(
        db.query(ImportRow.state, func.count())
        .filter(ImportRow.batch_id == batch.id).group_by(ImportRow.state).all())
    matched = (ImportRow.state == MATCHED)
    skipped, encrypted, ready, auto_picked = db.query(
        func.coalesce(func.sum(case((ImportRow.skipped, 1), else_=0)), 0),
        func.coalesce(func.sum(case((ImportRow.encrypted, 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(matched, ImportRow.skipped.is_(False)), 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(matched, ImportRow.auto_picked.is_(True)), 1), else_=0)), 0),
    ).filter(ImportRow.batch_id == batch.id).one()
    return {
        "id": batch.id,
        "created_at": _aware(batch.created_at).isoformat() if batch.created_at else None,
        "expires_at": _aware(batch.expires_at).isoformat(),
        "total": sum(counts.values()),
        "counts": counts,
        "skipped": int(skipped),
        "encrypted": int(encrypted),
        "ready": int(ready),  # matched and not skipped: what "Import N books" imports
        "auto_picked": int(auto_picked),
    }


def create_batch() -> dict:
    purge_expired()
    now = _now()
    batch = ImportBatch(created_at=now, updated_at=now, expires_at=now + RETENTION)
    db.add(batch)
    db.commit()
    return summary(batch)


def list_batches() -> list:
    purge_expired()
    batches = (
        db.query(ImportBatch).filter(ImportBatch.expires_at >= _now())
        .order_by(ImportBatch.id.desc()).limit(MAX_BATCHES_LISTED).all())
    result = [summary(b) for b in batches]
    db.rollback()
    return result


def get_summary(batch_id: int) -> dict:
    try:
        return summary(_get(batch_id))
    finally:
        db.rollback()


def delete_batch(batch_id: int) -> None:
    try:
        _get(batch_id)
        db.query(ImportRow).filter(ImportRow.batch_id == batch_id).delete(synchronize_session=False)
        db.query(ImportBatch).filter(ImportBatch.id == batch_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.rollback()


# ─── rows ───────────────────────────────────────────────────────────────────

def upsert_rows(batch_id: int, raw_rows: list) -> dict:
    """Add rows the batch does not have yet; refresh the metadata of ones it does.

    Idempotent by row key, so a retry or a re-dropped folder never duplicates a
    row or resets a decision. Held under a per-batch lock so two concurrent
    drops of the same folder cannot both insert the same key.
    """
    if len(raw_rows) > MAX_ROWS_PER_CALL:
        raise ValueError(f"at most {MAX_ROWS_PER_CALL} rows per call")
    rows = {}
    for i, raw in enumerate(raw_rows):
        clean = _clean_row(raw, i)
        rows[clean["row_key"]] = clean  # last one wins within a call
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = _get(batch_id)
        existing = {
            key: (row_id, state) for key, row_id, state in
            db.query(ImportRow.row_key, ImportRow.id, ImportRow.state)
            .filter(ImportRow.batch_id == batch_id, ImportRow.row_key.in_(list(rows))).all()}
        new = [dict(r, batch_id=batch_id) for key, r in rows.items() if key not in existing]
        updates = []
        for key, (row_id, state) in existing.items():
            if state in _REFRESHABLE:
                fields = {k: rows[key][k] for k in _METADATA if rows[key][k] is not None}
                if fields:
                    updates.append(dict(fields, id=row_id))
        if new:
            db.bulk_insert_mappings(ImportRow, [
                dict(r, decision=AUTO, skipped=False, encrypted=False, auto_picked=False,
                     candidates_count=0, confidence=0) for r in new])
        if updates:
            db.bulk_update_mappings(ImportRow, updates)
        _touch(batch)
        db.commit()
        return {"created": len(new), "existing": len(existing)}
    finally:
        db.rollback()


def _filtered(batch_id: int, state: str | None, q: str | None,
              skipped: bool | None = None, auto_picked: bool | None = None):
    query = db.query(ImportRow).filter(ImportRow.batch_id == batch_id)
    if state:
        query = query.filter(ImportRow.state == state)
    if skipped is not None:
        query = query.filter(ImportRow.skipped.is_(skipped))
    if auto_picked is not None:
        query = query.filter(ImportRow.auto_picked.is_(auto_picked))
    if q:
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(*[
            func.lower(col).like(like, escape="\\")
            for col in (ImportRow.path, ImportRow.title, ImportRow.author)]))
    return query


def _row_dict(row: ImportRow) -> dict:
    return {name: getattr(row, name) for name in _ROW_FIELDS}


def list_rows(batch_id: int, state: str | None = None, q: str | None = None,
              cursor: int | None = None, limit: int = 100,
              skipped: bool | None = None, auto_picked: bool | None = None) -> dict:
    limit = max(1, min(limit, MAX_PAGE))
    try:
        _get(batch_id)
        query = _filtered(batch_id, state, q, skipped, auto_picked)
        if cursor:
            query = query.filter(ImportRow.id > cursor)
        rows = query.order_by(ImportRow.id).limit(limit + 1).all()
        page = rows[:limit]
        return {
            "rows": [_row_dict(r) for r in page],
            "next_cursor": page[-1].id if len(rows) > limit else None,
        }
    finally:
        db.rollback()


def get_row(batch_id: int, row_id: int) -> dict:
    try:
        _get(batch_id)
        row = db.query(ImportRow).filter(
            ImportRow.batch_id == batch_id, ImportRow.id == row_id).one_or_none()
        if row is None:
            raise ImportRowNotFound(f"Row {row_id} not found.")
        return _row_dict(row)
    finally:
        db.rollback()


def existing_olids(olids: list) -> list:
    """Which of these editions are already in the library."""
    from lenny.core.models import Item
    try:
        return sorted(o for (o,) in db.query(Item.openlibrary_edition).filter(
            Item.openlibrary_edition.in_(olids)).all()) if olids else []
    finally:
        db.rollback()


def update_row(batch_id: int, row_id: int, skipped: bool | None = None,
               olid: int | None = None, encrypted: bool | None = None) -> dict:
    """Apply one admin decision to one row.

    A new edition on a row that is already imported moves the stored book to
    that edition (LennyAPI.rename_item: files copied, then the item repointed;
    loans survive because they follow the item, not the edition).
    """
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = _get(batch_id)
        row = db.query(ImportRow).filter(
            ImportRow.batch_id == batch_id, ImportRow.id == row_id).one_or_none()
        if row is None:
            raise ImportRowNotFound(f"Row {row_id} not found.")
        if row.state == IMPORTING:
            raise ImportRowBusy("Wait for this upload to finish.")

        # Work on the stored book first. Both calls open and commit their own
        # transactions, so release this one (and its lock) around them, then
        # re-take the lock and re-read the row before applying the row changes.
        imported = row.state == IMPORTED and row.olid
        rename = imported and olid is not None and olid != row.olid
        reflag = imported and encrypted is not None and encrypted != row.encrypted
        if rename or reflag:
            from lenny.core.api import LennyAPI  # lazy: api imports a lot
            current = row.olid
            db.rollback()
            if rename:
                LennyAPI.rename_item(current, olid)
                current = olid
            if reflag:
                LennyAPI.update_item(current, encrypted=encrypted)
            _xact_lock(f"import_batch:{batch_id}")
            batch = _get(batch_id)
            row = db.query(ImportRow).filter(ImportRow.id == row_id).one()

        if olid is not None and olid != row.olid:
            if row.state != IMPORTED:
                row.state = MATCHED
            row.olid, row.decision, row.auto_picked, row.error = olid, USER, False, None
        if skipped is not None:
            row.skipped = skipped
        if encrypted is not None:
            row.encrypted = encrypted
        _touch(batch)
        db.commit()
        return _row_dict(row)
    finally:
        db.rollback()


def bulk_update(batch_id: int, action: str, state: str | None = None,
                q: str | None = None, value: bool | None = None,
                skipped: bool | None = None, auto_picked: bool | None = None) -> int:
    """Apply an action to every row matching a filter; never takes a list of ids."""
    if action == "set_encrypted":
        if not isinstance(value, bool):
            raise ValueError("'value' must be a boolean for set_encrypted")
        change, differs = {ImportRow.encrypted: value}, ImportRow.encrypted.is_not(value)
    elif action in ("skip", "unskip"):
        change, differs = {ImportRow.skipped: action == "skip"}, ImportRow.skipped.is_not(action == "skip")
    else:
        raise ValueError("'action' must be one of skip, unskip, set_encrypted")
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = _get(batch_id)
        query = _filtered(batch_id, state, q, skipped, auto_picked)
        if action != "set_encrypted":
            query = query.filter(ImportRow.state.notin_(_SETTLED))
        else:
            # Imported rows already have an item; flip its flag in step with the row.
            from lenny.core.models import Item
            imported = query.filter(ImportRow.state == IMPORTED, ImportRow.olid.is_not(None))
            db.query(Item).filter(Item.openlibrary_edition.in_(
                imported.with_entities(ImportRow.olid).scalar_subquery()
            )).update({"encrypted": value}, synchronize_session=False)
        # Only rows that actually change, so the count reports real work.
        updated = query.filter(differs).update(change, synchronize_session=False)
        _touch(batch)
        db.commit()
        return updated
    finally:
        db.rollback()
