"""Preloaded free books: browse what is available, pick some, load them.

    catalog.py   what can actually be downloaded (Open Library records ∩ the
                 archive.org preload set), searchable, with collections
    service.py   resolve a pick (ids, pasted list, collection) and load it

The downloader, EPUB check and upload live in lenny/core/standardebooks.py and
are shared with the CLI (`make preload`).
"""
