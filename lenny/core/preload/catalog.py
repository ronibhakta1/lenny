"""The catalog of free books Lenny can preload.

Open Library knows every Standard Ebooks title (864 at the time of writing) but
the archive.org item Lenny downloads from holds only some of them (190). Showing
the first list would offer books that fail to download, so the catalog is the
intersection: a record is `available` only if its file is in the item.

Built from one archive.org call plus ~9 Open Library pages and held in this
process for CATALOG_TTL. If a rebuild fails the old copy keeps being served, so
a brief outage does not blank the page. ponytail: per process, no shared store;
every worker rebuilds once per TTL (about 10 requests), which is fine for this
size. Move it into the Cache table if workers multiply or the catalog grows.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from lenny.core.openlibrary import OpenLibrary
from lenny.core.standardebooks import StandardEbooks, clean_source_id

logger = logging.getLogger(__name__)

CATALOG_TTL = 6 * 3600
ARCHIVE_ITEM = "https://archive.org/metadata/lenny-open-access-preloads"
MAX_SUBJECTS = 6
MAX_PAGE = 100
DEFAULT_PAGE = 24

# name → substrings looked for in a book's subjects (case-insensitive).
COLLECTIONS = {
    "classic-fiction": ("Classic fiction", ("fiction classics", "classic", "literary fiction")),
    "science-fiction": ("Science fiction", ("science fiction", "sci-fi")),
    "mystery": ("Mystery & detective", ("mystery", "detective", "crime")),
    "adventure": ("Adventure", ("adventure",)),
    "romance": ("Romance", ("romance", "love stories")),
    "poetry": ("Poetry", ("poetry", "poems")),
    "plays": ("Plays & drama", ("drama", "plays")),
    "philosophy": ("Philosophy", ("philosophy",)),
    "childrens": ("Children's", ("children", "juvenile")),
    "horror": ("Horror & gothic", ("horror", "gothic", "ghost")),
}


class CatalogUnavailable(Exception):
    """Neither archive.org nor Open Library could be reached and nothing is cached."""


_lock = threading.Lock()
_cached = {"built": 0.0, "books": [], "known": 0}


def _archive_files() -> dict:
    """{identifier file name (no .epub): size in bytes} for the preload set."""
    resp = httpx.get(ARCHIVE_ITEM, timeout=StandardEbooks.HTTP_TIMEOUT, headers=_headers())
    resp.raise_for_status()
    return {f["name"][:-5]: int(f.get("size") or 0)
            for f in resp.json().get("files", []) if f.get("name", "").endswith(".epub")}


ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
_ITEMS_QUERY = '(subject:"Standard eBooks" OR collection:standardebooks) AND mediatype:texts'


def _archive_item_names() -> set:
    """Identifiers of standalone archive.org items tagged Standard eBooks that carry an EPUB
    (one search, a hundred or so). Their name is the Standard Ebooks id with / as _."""
    resp = httpx.get(ARCHIVE_SEARCH, timeout=StandardEbooks.HTTP_TIMEOUT, headers=_headers(),
                     params={"q": _ITEMS_QUERY, "fl[]": ["identifier", "format"], "rows": 1000, "output": "json"})
    resp.raise_for_status()
    docs = resp.json().get("response", {}).get("docs", [])
    return {d["identifier"] for d in docs if d.get("identifier") and "EPUB" in (d.get("format") or [])}


def available_files() -> dict:
    """{name: size} of every EPUB archive.org can serve for a Standard Ebooks title: the files
    of the preload item (size known) plus standalone items (size unknown, 0). The standalone
    list is a bonus: if that search fails, the preload item alone is used."""
    files = _archive_files()
    try:
        extra = _archive_item_names()
    except (httpx.HTTPError, ValueError, OSError) as e:
        logger.warning("Could not list standalone archive.org items (%s)", e)
        extra = set()
    return {**{name: 0 for name in extra}, **files}


def _headers() -> dict:
    from lenny.core.client import LennyClient
    return LennyClient.HTTP_HEADERS


def _book(record, files: dict):
    """One catalog entry, or None if the record cannot be loaded."""
    source_id = clean_source_id(record.standardebooks_id)
    if not source_id:
        return None
    try:
        olid = int(record.olid)
    except (ValueError, AttributeError, TypeError, IndexError, KeyError):
        return None
    name = source_id.replace("/", "_")
    if name not in files:
        return None
    try:
        cover = record.cover_url
    except (IndexError, KeyError, AttributeError):
        cover = None
    return {
        "olid": olid, "title": record.get("title"), "authors": list(record.get("author_name") or []),
        "year": record.get("first_publish_year"), "cover_url": cover,
        "subjects": list(record.get("subject") or [])[:MAX_SUBJECTS],
        "source": "standardebooks", "source_id": source_id, "size": files[name],
    }


PAGE = 100
FETCH_WORKERS = 3
_FIELDS = ["title", "author_name", "first_publish_year", "subject", "id_standard_ebooks", "cover_i"]
_QUERY = "id_standard_ebooks:*"


def _records(pages: list) -> list:
    from lenny.core.openlibrary import OpenLibraryRecord
    return [OpenLibraryRecord(doc) for page in pages for doc in (page.get("docs", []) if isinstance(page, dict) else [])]


def _fetch_records() -> list:
    """Every Standard Ebooks record. The first page says how many there are, then the
    rest are fetched a few at a time (each page takes seconds), roughly 3x quicker than in turn."""
    first = OpenLibrary.search_json(_QUERY, fields=_FIELDS, page=1, limit=PAGE)
    total = int(first.get("numFound") or 0) if isinstance(first, dict) else 0
    pages = [first]
    if total > PAGE:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="preload-catalog") as pool:
            pages += list(pool.map(lambda n: OpenLibrary.search_json(_QUERY, fields=_FIELDS, page=n, limit=PAGE),
                                   range(2, (total + PAGE - 1) // PAGE + 1)))
    return _records(pages)


def _build() -> dict:
    files = available_files()
    books, known = [], 0
    for record in _fetch_records():
        if not clean_source_id(record.standardebooks_id):
            continue                      # junk in Open Library's field: not a Standard Ebooks id
        known += 1
        if (book := _book(record, files)):
            books.append(book)
    books.sort(key=lambda b: (b["title"] or "").lower())
    return {"built": time.monotonic(), "books": books, "known": known}


_refreshing = threading.Event()


def _refresh_in_background() -> None:
    """Rebuild without making anyone wait; the old catalog keeps being served meanwhile."""
    if _refreshing.is_set():
        return
    _refreshing.set()

    def work():
        try:
            fresh = _build()
            with _lock:
                _cached.update(fresh)
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.warning("Could not refresh the preload catalog (%s)", e)
        finally:
            _refreshing.clear()
    threading.Thread(target=work, name="preload-catalog-refresh", daemon=True).start()


def warm() -> None:
    """Build the catalog ahead of the first visitor (called at startup, in a thread)."""
    try:
        get()
    except CatalogUnavailable:
        pass


def get(now=time.monotonic) -> dict:
    """The catalog: {books, known, built}.

    Built on first use (and at startup, see warm). Once it is older than CATALOG_TTL
    the old copy is still returned at once while a new one is built in the background,
    so nobody waits 30+ seconds twice. Only when there is no copy at all does a caller wait."""
    if _cached["books"]:
        if now() - _cached["built"] >= CATALOG_TTL:
            _refresh_in_background()
        return _cached
    with _lock:  # the first build: one at a time; others wait and then reuse it
        if _cached["books"]:
            return _cached
        try:
            _cached.update(_build())
        except (httpx.HTTPError, ValueError, OSError) as e:
            logger.warning("Could not build the preload catalog (%s)", e)
            raise CatalogUnavailable("Open Library or archive.org is not answering; try again shortly.") from e
        return _cached


def _matches(book: dict, q: str) -> bool:
    hay = " ".join([book["title"] or "", *book["authors"], *book["subjects"]]).lower()
    return all(word in hay for word in q.lower().split())


def in_collection(book: dict, collection: str) -> bool:
    keywords = COLLECTIONS[collection][1]
    subjects = " ".join(book["subjects"]).lower()
    return any(k in subjects for k in keywords)


def search(q: str | None = None, collection: str | None = None, cursor: int = 0,
           limit: int = DEFAULT_PAGE, hide: set | None = None) -> dict:
    """A page of books. `hide` is a set of olids to leave out (e.g. already in the library)."""
    if collection is not None and collection not in COLLECTIONS:
        raise ValueError(f"'collection' must be one of {', '.join(COLLECTIONS)}")
    cat = get()
    found = [b for b in cat["books"]
             if (not q or _matches(b, q)) and (not collection or in_collection(b, collection))
             and not (hide and b["olid"] in hide)]
    cursor, limit = max(0, cursor), max(1, min(limit, MAX_PAGE))
    page = found[cursor:cursor + limit]
    return {"books": page, "total": len(found), "available": len(cat["books"]), "known": cat["known"],
            "next_cursor": cursor + limit if cursor + limit < len(found) else None}


def collections(existing: set) -> list:
    """Named groups with how many books each holds and how many you already have."""
    cat = get()
    out = []
    for cid, (name, _) in COLLECTIONS.items():
        members = [b for b in cat["books"] if in_collection(b, cid)]
        if members:
            out.append({"id": cid, "name": name, "count": len(members),
                        "in_library": sum(b["olid"] in existing for b in members)})
    return out


def find(olids=(), source_ids=(), collection: str | None = None) -> tuple:
    """Resolve a pick to catalog books. Returns (books, unknown) where unknown
    lists every requested olid / source id the catalog does not have."""
    cat = get()
    by_olid = {b["olid"]: b for b in cat["books"]}
    by_source = {b["source_id"]: b for b in cat["books"]}
    picked, unknown = {}, []
    for o in olids:
        (picked.__setitem__(o, by_olid[o]) if o in by_olid else unknown.append(o))
    for s in source_ids:
        book = by_source.get(s)
        (picked.__setitem__(book["olid"], book) if book else unknown.append(s))
    if collection:
        if collection not in COLLECTIONS:
            raise ValueError(f"'collection' must be one of {', '.join(COLLECTIONS)}")
        for b in cat["books"]:
            if in_collection(b, collection):
                picked[b["olid"]] = b
    return list(picked.values()), unknown
