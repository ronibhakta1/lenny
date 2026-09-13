"""Admin-facing item search/listing logic.

Unlike core/admin_loans.py's title resolution, this never calls out to Open
Library at read time: `title`/`author` are denormalized onto `Item` at
add-time (see LennyAPI.add), so this is a plain local filter/sort/paginate —
the pattern that live-lookup-per-request one was missing.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import or_

from lenny.core.db import session as db
from lenny.core.models import Item

_MAX_ITEMS_RETURNED = 5000
_DEFAULT_LIMIT = 50

_SORT_COLUMNS = {
    "title": Item.title,
    "author": Item.author,
    "created_at": Item.created_at,
}
VALID_SORTS = tuple(_SORT_COLUMNS)


def _shape_row(item: Item) -> dict:
    edition_int = item.openlibrary_edition
    return {
        "id": item.id,
        "edition_key": f"OL{edition_int}M" if edition_int else "",
        "title": item.title or "",
        "author": item.author or "",
        "encrypted": item.encrypted,
        "formats": item.formats.name if item.formats else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def query_items_for_admin(
    *,
    q: Optional[str] = None,
    encrypted: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    sort: str = "title",
    order: str = "asc",
) -> tuple[list[dict], int]:
    """Filtered, paginated item listing for the admin UI's Create Loan
    picker and Library search.

    Returns ``(items, total)`` where *total* is the count matching the
    filters (before limit/offset).

    ``q`` matches a leading prefix of title OR author (case-insensitive) —
    a '%q%' contains-search would fall back to a sequential scan since the
    backing index (idx_items_title/idx_items_author) uses varchar_pattern_ops,
    which only serves a leading-anchor LIKE. Add pg_trgm if contains-search
    is ever actually needed.

    Raises ValueError on an invalid sort/order (the route maps to 400).
    """
    if sort not in _SORT_COLUMNS:
        raise ValueError(f"invalid sort: {sort!r}")
    if order not in ("asc", "desc"):
        raise ValueError(f"invalid order: {order!r}")

    cap = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_ITEMS_RETURNED))
    off = max(0, int(offset or 0))

    query = db.query(Item)
    if encrypted is not None:
        query = query.filter(Item.encrypted == encrypted)
    if q:
        needle = f"{q}%"
        query = query.filter(or_(Item.title.ilike(needle), Item.author.ilike(needle)))

    total = query.count()

    col = _SORT_COLUMNS[sort]
    primary = col.desc() if order == "desc" else col.asc()
    # Stable tiebreak by id so pagination is deterministic when sort keys tie.
    rows = query.order_by(primary, Item.id.desc()).offset(off).limit(cap).all()

    return [_shape_row(item) for item in rows], total
