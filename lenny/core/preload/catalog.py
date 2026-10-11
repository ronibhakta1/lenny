"""The catalog of free books Lenny can preload.

Open Library knows every Standard Ebooks title (864 at the time of writing) but
the archive.org item Lenny downloads from holds only some of them (190). Showing
the first list would offer books that fail to download, so the catalog is the
intersection: a record is `available` only if its file is in the item.

Built from one archive.org call plus ~9 Open Library pages and held in this
process. It follows the archive.org item by itself: a small probe (the item's
"last updated" number, once a minute at most, never on the request path) notices
when books were added and rebuilds in the background, so a book uploaded to the
item shows up within a minute or so with no change on our side; CATALOG_TTL is
only the backstop for what the probe cannot see (Open Library data, other items).
If a rebuild fails the old copy keeps being served, so a brief outage does not blank the page. ponytail: per process, no shared store;
every worker rebuilds once per TTL (about 10 requests), which is fine for this
size. Move it into the Cache table if workers multiply or the catalog grows.
"""

import logging
import re
import threading
from html import unescape
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import httpx

from lenny.core.openlibrary import OpenLibrary
from lenny.core.standardebooks import StandardEbooks, clean_source_id

logger = logging.getLogger(__name__)

CATALOG_TTL = 3600
PROBE_EVERY = 60          # seconds between looks at the item's "last updated"
NEW_DAYS = 14             # a book added to the item this recently is flagged new
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
_cached = {"built": 0.0, "books": [], "known": 0, "updated": None, "probed": 0.0}


def _archive_listing() -> tuple:
    """The preload item: ({file name (no .epub): size}, {file name: time it was added, epoch}, item last updated)."""
    resp = httpx.get(ARCHIVE_ITEM, timeout=StandardEbooks.HTTP_TIMEOUT, headers=_headers())
    resp.raise_for_status()
    data = resp.json()
    epubs = [f for f in data.get("files", []) if f.get("name", "").endswith(".epub")]
    sizes = {f["name"][:-5]: int(f.get("size") or 0) for f in epubs}
    added = {f["name"][:-5]: int(f["mtime"]) for f in epubs if str(f.get("mtime") or "").isdigit()}
    return sizes, added, data.get("item_last_updated")


def _probe_updated():
    """The item's "last updated" number (a few bytes), or None if archive.org cannot say right now."""
    try:
        resp = httpx.get(f"{ARCHIVE_ITEM}/item_last_updated", timeout=StandardEbooks.HTTP_TIMEOUT, headers=_headers())
        resp.raise_for_status()
        return resp.json().get("result")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
_ITEMS_QUERY = '(subject:"Standard eBooks" OR subject:standardebooks OR collection:standardebooks) AND mediatype:texts'


def _archive_item_names() -> set:
    """Identifiers of standalone archive.org items tagged Standard eBooks that carry an EPUB
    (one search, a hundred or so). Their name is the Standard Ebooks id with / as _."""
    resp = httpx.get(ARCHIVE_SEARCH, timeout=StandardEbooks.HTTP_TIMEOUT, headers=_headers(),
                     params={"q": _ITEMS_QUERY, "fl[]": ["identifier", "format"], "rows": 1000, "output": "json"})
    resp.raise_for_status()
    docs = resp.json().get("response", {}).get("docs", [])
    return {d["identifier"] for d in docs if d.get("identifier") and "EPUB" in (d.get("format") or [])}


def available_files() -> tuple:
    """({name: size}, {name: added epoch}, item last updated) for every EPUB archive.org can serve
    for a Standard Ebooks title: the files of the preload item (size and date known) plus standalone
    items (size unknown, 0, no date). The standalone list is a bonus: if that search fails, the
    preload item alone is used."""
    files, added, updated = _archive_listing()
    try:
        extra = _archive_item_names()
    except (httpx.HTTPError, ValueError, OSError) as e:
        logger.warning("Could not list standalone archive.org items (%s)", e)
        extra = set()
    return {**{name: 0 for name in extra}, **files}, added, updated


def _headers() -> dict:
    from lenny.core.client import LennyClient
    return LennyClient.HTTP_HEADERS


def _iso(epoch) -> str | None:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if isinstance(epoch, int) else None


def _book(record, files: dict, added: dict | None = None):
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
        "added_at": _iso((added or {}).get(name)),
    }


PAGE = 100
FETCH_WORKERS = 3
_FIELDS = ["title", "author_name", "first_publish_year", "subject", "id_standard_ebooks", "cover_i"]
_QUERY = "id_standard_ebooks:*"


def _records(pages: list) -> list:
    from lenny.core.openlibrary import OpenLibraryRecord
    return [OpenLibraryRecord(doc) for page in pages for doc in (page.get("docs", []) if isinstance(page, dict) else [])]


def _page(n: int) -> dict:
    """One page of records, retried: Open Library answers 500 now and then and a catalog
    of 9 pages should not fail for one of them."""
    for attempt in range(3):
        try:
            return OpenLibrary.search_json(_QUERY, fields=_FIELDS, page=n, limit=PAGE)
        except (httpx.HTTPError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def _fetch_records() -> list:
    """Every Standard Ebooks record. The first page says how many there are, then the
    rest are fetched a few at a time (each page takes seconds), roughly 3x quicker than in turn."""
    first = _page(1)
    total = int(first.get("numFound") or 0) if isinstance(first, dict) else 0
    pages = [first]
    if total > PAGE:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS, thread_name_prefix="preload-catalog") as pool:
            pages += list(pool.map(_page, range(2, (total + PAGE - 1) // PAGE + 1)))
    return _records(pages)


def _build() -> dict:
    files, added, updated = available_files()
    books, known = [], 0
    for record in _fetch_records():
        if not clean_source_id(record.standardebooks_id):
            continue                      # junk in Open Library's field: not a Standard Ebooks id
        known += 1
        if (book := _book(record, files, added)):
            books.append(book)
    books.sort(key=lambda b: (b["title"] or "").lower())
    return {"built": time.monotonic(), "books": books, "known": known, "updated": updated, "probed": time.monotonic()}


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


_probing = threading.Event()


def _probe_in_background(now=time.monotonic) -> None:
    """At most once a minute, in a thread: has the archive.org item changed since the catalog was
    built? If so rebuild (still in the background, the old list is served until it is ready)."""
    if now() - _cached["probed"] < PROBE_EVERY or _probing.is_set():
        return
    _probing.set()
    _cached["probed"] = now()

    def work():
        try:
            updated = _probe_updated()
            if updated is not None and updated != _cached["updated"]:
                logger.info("The preload item changed (%s -> %s): refreshing the catalog", _cached["updated"], updated)
                _refresh_in_background()
        finally:
            _probing.clear()
    threading.Thread(target=work, name="preload-catalog-probe", daemon=True).start()


def refresh() -> bool:
    """Rebuild now, in the background (the Refresh button). False if one is already running."""
    if _refreshing.is_set():
        return False
    _refresh_in_background()
    return True


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
        else:
            _probe_in_background(now)
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


SORTS = ("title", "added_desc")


def is_new(book: dict, now: datetime | None = None) -> bool:
    """Added to the archive.org item in the last NEW_DAYS days (books from standalone items carry no date)."""
    if not book.get("added_at"):
        return False
    return datetime.fromisoformat(book["added_at"]) >= (now or datetime.now(timezone.utc)) - timedelta(days=NEW_DAYS)


def search(q: str | None = None, collection: str | None = None, cursor: int = 0,
           limit: int = DEFAULT_PAGE, hide: set | None = None, sort: str = "title", new_only: bool = False) -> dict:
    """A page of books. `hide` is a set of olids to leave out (e.g. already in the library);
    `sort` is title or added_desc (newest additions first); `new_only` keeps only recent additions."""
    if collection is not None and collection not in COLLECTIONS:
        raise ValueError(f"'collection' must be one of {', '.join(COLLECTIONS)}")
    if sort not in SORTS:
        raise ValueError(f"'sort' must be one of {', '.join(SORTS)}")
    cat = get()
    now = datetime.now(timezone.utc)
    found = [b for b in cat["books"]
             if (not q or _matches(b, q)) and (not collection or in_collection(b, collection))
             and not (hide and b["olid"] in hide) and (not new_only or is_new(b, now))]
    if sort == "added_desc":
        found.sort(key=lambda b: (b.get("added_at") or "", (b["title"] or "").lower()), reverse=True)
    cursor, limit = max(0, cursor), max(1, min(limit, MAX_PAGE))
    page = [dict(b, is_new=is_new(b, now)) for b in found[cursor:cursor + limit]]
    return {"books": page, "total": len(found), "available": len(cat["books"]), "known": cat["known"],
            "new": sum(is_new(b, now) for b in cat["books"]), "updated_at": _iso(cat.get("updated")),
            "refreshing": _refreshing.is_set(),
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


# ─── one book's full details ─────────────────────────────────────────────────

DETAILS_TTL = 6 * 3600
DETAILS_MAX = 256
_details: "dict[int, tuple]" = {}
_details_lock = threading.Lock()


def _get_json(url: str):
    """One Open Library JSON document, through the shared circuit breaker. Raises httpx errors."""
    from lenny.core.upstream import open_library

    def fetch():
        resp = httpx.get(url, timeout=StandardEbooks.HTTP_TIMEOUT, headers=_headers(), follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    try:
        return open_library.call(("preload-details", url), fetch)
    except Exception as e:                       # the breaker raises its own error type when open
        raise httpx.HTTPError(str(e)) from e


def _text(value) -> str | None:
    """Open Library descriptions are a string or {"type": ..., "value": ...}."""
    if isinstance(value, dict):
        value = value.get("value")
    text = unescape(re.sub(r"<[^>]*>", " ", str(value or "")))     # Standard Ebooks blurbs carry HTML
    text = " ".join(text.split())
    return text[:2000] or None


def details(olid: int) -> dict | None:
    """Everything worth showing about one catalog book, or None if it is not in the catalog.

    The catalog row (title, authors, cover, size, how it was added) plus the edition and its work
    from Open Library (publisher, date, pages, ISBN, languages, description, all subjects), cached for
    DETAILS_TTL. If Open Library cannot answer, the catalog row is returned with `details_loaded: False`
    rather than an error: the sheet is still useful."""
    book = next((b for b in get()["books"] if b["olid"] == olid), None)
    if book is None:
        return None
    now = time.monotonic()
    with _details_lock:
        hit = _details.get(olid)
    if hit and now - hit[0] < DETAILS_TTL:
        extra = hit[1]
    else:
        extra = _fetch_details(olid)
        if extra.get("details_loaded"):
            with _details_lock:
                _details[olid] = (now, extra)
                while len(_details) > DETAILS_MAX:
                    _details.pop(next(iter(_details)))
    sid = book["source_id"]
    cover = book.get("cover_url")
    return {
        **book, **extra,
        "cover_url_large": cover.replace("-M.jpg", "-L.jpg") if cover else None,
        "links": {"open_library": f"https://openlibrary.org/books/OL{olid}M",
                  "standard_ebooks": f"https://standardebooks.org/ebooks/{sid}",
                  "archive_org": f"https://archive.org/details/{sid.replace('/', '_')}"},
    }


def _fetch_details(olid: int) -> dict:
    try:
        edition = _get_json(f"https://openlibrary.org/books/OL{olid}M.json")
    except (httpx.HTTPError, ValueError):
        return {"details_loaded": False}
    langs = [str(l.get("key", "")).rsplit("/", 1)[-1] for l in edition.get("languages") or [] if isinstance(l, dict)]
    out = {
        "details_loaded": True, "publisher": (edition.get("publishers") or [None])[0],
        "publish_date": edition.get("publish_date"), "pages": edition.get("number_of_pages"),
        "isbn": (edition.get("isbn_13") or edition.get("isbn_10") or [None])[0], "languages": [l for l in langs if l],
        "description": _text(edition.get("description")), "all_subjects": [],
    }
    work_key = ((edition.get("works") or [{}])[0] or {}).get("key", "")
    if work_key.startswith("/works/OL") and work_key.endswith("W"):
        try:
            work = _get_json(f"https://openlibrary.org{work_key}.json")
            out["description"] = out["description"] or _text(work.get("description"))
            out["all_subjects"] = [str(x) for x in (work.get("subjects") or [])][:30]
        except (httpx.HTTPError, ValueError):
            pass                                  # the edition's own facts are still worth showing
    return out
