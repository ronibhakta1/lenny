"""Bulk EPUB import: drop a folder, match to Open Library, review, import.

    batches.py   server-side batch and row state (30-day retention)
    matcher.py   Open Library search, scoring, auto-pick decisions
    limits.py    request pacing and the numbers the page reads
    jobs.py      background matching of a batch
    importer.py  store one matched row's EPUB

The older single-source importers (briet.py, standardebooks.py) and their
progress record (imports.py) stay where they are.
"""

from lenny.core.bulk_import import batches  # registers its tables with Base.metadata

__all__ = ["batches"]
