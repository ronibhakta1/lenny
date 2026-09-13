"""
One-off backfill: populate Item.title/author for rows added before those
columns existed. Only touches rows where title IS NULL, so it's safe to
re-run (e.g. after an OL outage left some rows unresolved).

Batches editions into a single Open Library query each, same shape as
core/admin_loans.py's `_resolve_titles`. Must run inside the lenny_api
container.

    make backfill-item-titles
"""

import sys

from lenny.core.db import session as db
from lenny.core.models import Item
from lenny.core.openlibrary import OpenLibrary

BATCH_SIZE = 100


def _resolve_batch(edition_ids: list[int]) -> dict[int, tuple[str, str]]:
    olid_query = " OR ".join(f"OL{eid}M" for eid in edition_ids)
    query = f"edition_key:({olid_query})"
    try:
        records = OpenLibrary.search(query=query, fields=["title", "author_name", "edition_key"])
    except Exception as exc:
        print(f"[x] OL lookup failed for batch of {len(edition_ids)}: {exc}", file=sys.stderr)
        return {}

    out: dict[int, tuple[str, str]] = {}
    for rec in records:
        try:
            eid = int(rec.olid)
        except (AttributeError, TypeError, ValueError):
            continue
        title = getattr(rec, "title", None) or None
        authors = getattr(rec, "author_name", None) or []
        author = ", ".join(authors) if authors else None
        out[eid] = (title, author)
    return out


if __name__ == "__main__":
    items = db.query(Item).filter(Item.title.is_(None)).all()
    if not items:
        print("Nothing to backfill.")
        sys.exit(0)

    by_edition = {item.openlibrary_edition: item for item in items}
    edition_ids = list(by_edition)
    updated = 0

    for i in range(0, len(edition_ids), BATCH_SIZE):
        batch = edition_ids[i:i + BATCH_SIZE]
        for eid, (title, author) in _resolve_batch(batch).items():
            item = by_edition[eid]
            item.title = title
            item.author = author
            updated += 1
        db.commit()
        print(f"[...] {min(i + BATCH_SIZE, len(edition_ids))}/{len(edition_ids)} editions processed")

    print(f"[done] backfilled {updated} item(s); {len(edition_ids) - updated} had no OL match.")
