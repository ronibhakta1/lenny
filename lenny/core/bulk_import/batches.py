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
    SmallInteger, String, UniqueConstraint, and_, case, false, func, or_,
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
ORDERS = ("id", "imported_at_desc")

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
    # sha1(path|size|mtime); an imported row's key gets "#<id>" appended, so the same file can
    # be dropped again and become a fresh row while the imported one stays in the history.
    row_key = Column(String(64), nullable=False)
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
    match_year = Column(SmallInteger, nullable=True)

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
    # The book's file is already on the server (staging.py), so importing it needs
    # no browser: nothing has to stay connected, survive a reload or a backgrounded app.
    staged = Column(Boolean, nullable=False, default=False, server_default=false())
    imported_at = Column(DateTime(timezone=True), nullable=True)  # set when the row becomes imported


_ROW_FIELDS = (
    "id", "path", "size", "mtime", "state", "decision", "skipped", "encrypted", "olid",
    "auto_picked", "candidates_count", "confidence", "match_title", "match_author", "match_year", "isbn", "title", "author",
    "language", "publisher", "year", "error", "staged", "imported_at",
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
    _drop_staged(batches=ids)
    return len(ids)


def _drop_staged(batches=(), rows=()):
    """Delete uploaded copies after their rows are gone (best effort: a leftover
    object is only wasted space, never a wrong book)."""
    from lenny.core.bulk_import import staging
    staging.drop(batches=batches, rows=rows)


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
    skipped, encrypted, ready, auto_picked, staged, unstaged = db.query(
        func.coalesce(func.sum(case((ImportRow.skipped, 1), else_=0)), 0),
        func.coalesce(func.sum(case((ImportRow.encrypted, 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(matched, ImportRow.skipped.is_(False)), 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(matched, ImportRow.auto_picked.is_(True)), 1), else_=0)), 0),
        func.coalesce(func.sum(case((ImportRow.staged, 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(matched, ImportRow.skipped.is_(False), ImportRow.staged.is_(False)), 1), else_=0)), 0),
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
        "staged": int(staged),            # files already on the server
        "unstaged": int(unstaged),        # matched, not skipped, file not on the server yet
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
        _drop_staged(batches=[batch_id])
    finally:
        db.rollback()


# ─── rows ───────────────────────────────────────────────────────────────────

def _reconcile(batch_id: int) -> dict:
    """Bring the saved list in line with the live library (call under the batch lock, then commit).

    The list is kept for 30 days, but books come and go in the library meanwhile. So:
    * a `duplicate` row whose book has since been deleted from the library is `matched` again,
      so the same file can be imported again;
    * an `imported` row from before keys were released gets its key freed ("#<id>" added), so
      dropping the same file again makes a new row instead of being swallowed by the old one.
    Returns how many of each, and how many imported rows no longer have their book."""
    from sqlalchemy import cast, select, String
    from lenny.core.models import Item
    in_library = select(Item.openlibrary_edition)
    reverted = db.query(ImportRow).filter(
        ImportRow.batch_id == batch_id, ImportRow.state == DUPLICATE, ImportRow.olid.is_not(None),
        ImportRow.olid.not_in(in_library)).update({"state": MATCHED, "error": None}, synchronize_session=False)
    released = db.query(ImportRow).filter(
        ImportRow.batch_id == batch_id, ImportRow.state == IMPORTED, ~ImportRow.row_key.contains("#")).update(
        {"row_key": ImportRow.row_key + "#" + cast(ImportRow.id, String)}, synchronize_session=False)
    gone = db.query(ImportRow).filter(
        ImportRow.batch_id == batch_id, ImportRow.state == IMPORTED, ImportRow.olid.is_not(None),
        ImportRow.olid.not_in(in_library)).count()
    return {"reverted": reverted, "released": released, "removed_from_library": gone}


def reconcile(batch_id: int) -> dict:
    """Public form of _reconcile: what changed since the library was last looked at.
    Safe to call as often as the page likes (a few indexed queries)."""
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = _get(batch_id)
        result = _reconcile(batch_id)
        if result["reverted"] or result["released"]:
            _touch(batch)
        db.commit()
        return result
    finally:
        db.rollback()


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
        _reconcile(batch_id)        # a file dropped again must not meet stale rows about books since deleted
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
        # `existing_states` says what the rows already in the list are (imported, matched, ...), so
        # the page can tell someone who drops the same folder again that nothing new was added.
        states = {}
        for _, state in existing.values():
            states[state] = states.get(state, 0) + 1
        return {"created": len(new), "existing": len(existing), "existing_states": states}
    finally:
        db.rollback()


def _filtered(batch_id: int, state: str | None, q: str | None,
              skipped: bool | None = None, auto_picked: bool | None = None, staged: bool | None = None,
              exclude_state: str | None = None):
    query = db.query(ImportRow).filter(ImportRow.batch_id == batch_id)
    if state:
        query = query.filter(ImportRow.state == state)
    if exclude_state:
        query = query.filter(ImportRow.state != exclude_state)
    if skipped is not None:
        query = query.filter(ImportRow.skipped.is_(skipped))
    if auto_picked is not None:
        query = query.filter(ImportRow.auto_picked.is_(auto_picked))
    if staged is not None:
        query = query.filter(ImportRow.staged.is_(staged))
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
              skipped: bool | None = None, auto_picked: bool | None = None,
              staged: bool | None = None, offset: int | None = None,
              with_total: bool = False, exclude_state: str | None = None, order: str = "id") -> dict:
    """A page of rows in id order. Two ways to page: `cursor` (rows after this id; what the
    import and upload passes use) or `offset` (skip this many; for numbered pages).
    `total` (rows matching the filters, ignoring paging) is computed only when asked for
    (`with_total`, or whenever `offset` is given) since it costs a count; otherwise None."""
    if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise ValueError("'offset' must be a non-negative integer")
    if offset is not None and cursor:
        raise ValueError("Use either 'cursor' or 'offset', not both")
    if order not in ORDERS:
        raise ValueError(f"'order' must be one of {', '.join(ORDERS)}")
    if order != "id" and cursor:
        raise ValueError("'cursor' only works with the default order; use 'offset'")
    if exclude_state is not None and exclude_state not in STATES:
        raise ValueError(f"'exclude_state' must be one of {', '.join(STATES)}")
    limit = max(1, min(limit, MAX_PAGE))
    try:
        _get(batch_id)
        query = _filtered(batch_id, state, q, skipped, auto_picked, staged, exclude_state)
        total = query.count() if (with_total or offset is not None) else None
        if cursor:
            query = query.filter(ImportRow.id > cursor)
        # imported_at_desc is the history order: newest first, id breaks ties so offset pages are stable.
        query = query.order_by(ImportRow.imported_at.desc(), ImportRow.id.desc()) if order == "imported_at_desc" \
            else query.order_by(ImportRow.id)
        if offset:
            query = query.offset(offset)
        rows = query.limit(limit + 1).all()
        page = rows[:limit]
        olids = {r.olid for r in page if r.olid}
        have = set()
        if olids:   # which of these books are in the library right now (one query per page)
            from lenny.core.models import Item
            have = {o for (o,) in db.query(Item.openlibrary_edition).filter(Item.openlibrary_edition.in_(olids)).all()}
        return {
            "rows": [dict(_row_dict(r), in_library=r.olid in have) for r in page],
            "next_cursor": page[-1].id if len(rows) > limit else None,
            "total": total,
        }
    finally:
        db.rollback()


def delete_row(batch_id: int, row_id: int) -> dict:
    """Remove one row from the batch (the list entry only: an imported book stays
    in the library). Refused while it is importing. Returns the batch summary."""
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = _get(batch_id)
        row = db.query(ImportRow).filter(
            ImportRow.batch_id == batch_id, ImportRow.id == row_id).one_or_none()
        if row is None:
            raise ImportRowNotFound(f"Row {row_id} not found.")
        if row.state == IMPORTING:
            raise ImportRowBusy("Wait for this upload to finish.")
        staged = row.staged
        db.delete(row)
        _touch(batch)
        db.commit()
        if staged:
            _drop_staged(rows=[(batch_id, row_id)])
        return summary(batch)
    finally:
        db.rollback()


def _set_match(row: ImportRow, match: dict | None) -> None:
    match = match or {}
    row.match_title = (match.get("title") or "")[:_MAX["title"]] or None
    row.match_author = (match.get("author") or "")[:_MAX["author"]] or None
    year = match.get("year")
    row.match_year = year if isinstance(year, int) and not isinstance(year, bool) and 0 < year < 10000 else None


def update_row(batch_id: int, row_id: int, skipped: bool | None = None,
               olid: int | None = None, encrypted: bool | None = None, match: dict | None = None) -> dict:
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
            # The row must show the edition that was chosen, not the one it was matched to first.
            row.confidence = 0
            _set_match(row, match)
        elif olid is not None and match and row.match_title is None:
            _set_match(row, match)    # same edition re-sent: fills in details an older choice never stored
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
                skipped: bool | None = None, auto_picked: bool | None = None,
                row_ids: list | None = None, exclude_state: str | None = None) -> int:
    """Apply an action to every row matching a filter. Only `remove` also takes a short
    list of row ids (the rows a person ticked), at most MAX_ROWS_PER_CALL, combined with
    any filter; every other action works on filters alone."""
    if row_ids is not None:
        if action != "remove":
            raise ValueError("'row_ids' is only for the remove action")
        if (not isinstance(row_ids, list) or not row_ids or len(row_ids) > MAX_ROWS_PER_CALL
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in row_ids)):
            raise ValueError(f"'row_ids' must be a list of 1-{MAX_ROWS_PER_CALL} row ids")
    if action == "set_encrypted":
        if not isinstance(value, bool):
            raise ValueError("'value' must be a boolean for set_encrypted")
        change, differs = {ImportRow.encrypted: value}, ImportRow.encrypted.is_not(value)
    elif action in ("skip", "unskip"):
        change, differs = {ImportRow.skipped: action == "skip"}, ImportRow.skipped.is_not(action == "skip")
    elif action == "remove":
        change = differs = None
    else:
        raise ValueError("'action' must be one of skip, unskip, set_encrypted, remove")
    try:
        _xact_lock(f"import_batch:{batch_id}")
        batch = _get(batch_id)
        if exclude_state is not None and exclude_state not in STATES:
            raise ValueError(f"'exclude_state' must be one of {', '.join(STATES)}")
        query = _filtered(batch_id, state, q, skipped, auto_picked, exclude_state=exclude_state)
        if row_ids is not None:
            query = query.filter(ImportRow.id.in_(row_ids))
        if action == "remove":
            # A row being uploaded right now stays; imported ones go (the book stays in the library).
            query = query.filter(ImportRow.state != IMPORTING)
            staged = [(batch_id, i) for (i,) in query.filter(ImportRow.staged.is_(True)).with_entities(ImportRow.id).all()]
            removed = query.delete(synchronize_session=False)
            _touch(batch)
            db.commit()
            _drop_staged(rows=staged)
            return removed
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
