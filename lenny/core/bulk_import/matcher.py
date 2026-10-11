"""Match dropped EPUBs to Open Library editions.

Rows with an ISBN go to Open Library fifty at a time (one query for fifty
books). Rows without one, or whose ISBN found nothing, get one title/author
query each. Every call is paced (limits.Pacer), retried politely on 429, and
goes through the shared circuit breaker (upstream.open_library) so a down
Open Library costs nothing.

Outcome per row:
    matched       best candidate scores >= AUTO_MIN: it is picked (auto_picked)
    needs_review  best scores REVIEW_MIN..AUTO_MIN: the admin chooses
    no_match      nothing close, or no usable metadata

Open Library returns works, each with its best-matching edition(s); a candidate
is one such edition. Tie-break between equal scores: same language, then has an
ISBN, then the older record. ponytail: one edition per work is what search
returns; asking for more editions per work is the upgrade if choices are thin.
"""

import re
import time
import unicodedata
from difflib import SequenceMatcher

import requests

from lenny.core.bulk_import.limits import ISBN_BATCH, Pacer
from lenny.core.upstream import OL_TIMEOUT, open_library

AUTO_MIN = 90
REVIEW_MIN = 60
MAX_CANDIDATES = 10
MAX_RETRY_WAIT = 60
RETRIES = 3

SEARCH_URL = "https://openlibrary.org/search.json"
_FIELDS = ("key,title,author_name,first_publish_year,editions,editions.key,editions.title,"
           "editions.isbn,editions.publisher,editions.language,editions.publish_year")
_OLID = re.compile(r"/books/OL(\d+)M$")

pacer = Pacer()


class UpstreamError(Exception):
    """Open Library could not answer (down, breaker open, or still rate limiting)."""


# ─── talking to Open Library ────────────────────────────────────────────────

def _headers() -> dict:
    from lenny.core.openlibrary import ol_auth_headers
    return ol_auth_headers()


def _get(url: str, params: dict | None = None, retries: int = RETRIES):
    """GET with pacing and Retry-After backoff; returns the response (any status other than 429).

    Interactive calls (an admin waiting on a click) pass retries=0: fail at the
    first 429 instead of sleeping up to a minute."""
    for attempt in range(retries + 1):
        pacer.wait()
        resp = requests.get(url, params=params, headers=_headers(), timeout=OL_TIMEOUT)
        if resp.status_code != 429:
            return resp
        if attempt == retries:
            break
        try:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
        except ValueError:
            wait = 2 ** attempt
        time.sleep(min(max(wait, 1), MAX_RETRY_WAIT))
    raise UpstreamError("Open Library is rate limiting this server; try again in a few minutes.")


def _call(key, url, params=None, retries=RETRIES):
    def fetch():
        resp = _get(url, params, retries)
        resp.raise_for_status()
        return resp.json()
    try:
        return open_library.call(key, fetch)
    except (requests.exceptions.RequestException, OSError, ValueError) as e:
        raise UpstreamError("Open Library is not answering; try again shortly.") from e


def search(params: dict, retries: int = RETRIES) -> list:
    params = dict(params, fields=_FIELDS)
    return _call(("search", tuple(sorted(params.items()))), SEARCH_URL, params, retries).get("docs", [])


def lookup_edition(olid: int) -> dict | None:
    """The edition's basic facts, or None if Open Library has no such edition."""
    url = f"https://openlibrary.org/books/OL{int(olid)}M.json"
    def fetch():
        resp = _get(url, retries=0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    try:
        data = open_library.call(("edition", olid), fetch)
    except (requests.exceptions.RequestException, OSError, ValueError) as e:
        raise UpstreamError("Open Library is not answering; try again shortly.") from e
    if data is None:
        return None
    first = lambda v: (v or [None])[0]
    return {"olid": int(olid), "title": data.get("title"), "publisher": first(data.get("publishers")),
            "year": data.get("publish_date"), "isbn": first(data.get("isbn_13") or data.get("isbn_10"))}


# ─── candidates and scoring ─────────────────────────────────────────────────

def _first(values):
    return values[0] if values else None


def candidates_from(docs: list) -> list:
    """Flatten works → editions into candidate dicts (isbns kept for scoring)."""
    seen, out = set(), []
    for work in docs:
        for ed in (work.get("editions") or {}).get("docs", []):
            m = _OLID.search(ed.get("key") or "")
            if not m or int(m.group(1)) in seen:
                continue
            seen.add(int(m.group(1)))
            isbns = ed.get("isbn") or []
            out.append({
                "olid": int(m.group(1)), "title": ed.get("title") or work.get("title"),
                "authors": work.get("author_name") or [],
                "year": _first(ed.get("publish_year")) or work.get("first_publish_year"),
                "publisher": _first(ed.get("publisher")), "language": _first(ed.get("language")),
                "isbn": _first(isbns), "isbns": isbns,
            })
    return out


def _norm(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


SUBTITLE_CREDIT = 0.8  # "Dune: Messiah" is not "Dune": a subtitle-less match goes to review, never auto


def _main_title(title) -> str:
    return _norm(re.split(r"[:–—]| - ", str(title or ""))[0])


def _title_ratio(wanted, have) -> float:
    return max(_ratio(_norm(wanted), _norm(have)),
               SUBTITLE_CREDIT * _ratio(_main_title(wanted), _main_title(have)))


def _names(author) -> list:
    """Split "A; B", "A & B", "A and B"; flip "Last, First"."""
    names = []
    for part in re.split(r";|&|\band\b", str(author or "")):
        part = part.strip()
        if "," in part:
            last, _, first = part.partition(",")
            part = f"{first} {last}"
        if _norm(part):
            names.append(_norm(part))
    return names


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def score(row: dict, cand: dict) -> int:
    """0-100: how sure we are that `cand` is the book in `row`."""
    isbn = row.get("isbn")
    if isbn and isbn in cand.get("isbns", ()):
        return 100
    if not row.get("title"):
        return 0
    title = _title_ratio(row["title"], cand.get("title"))
    wanted = _names(row.get("author"))
    if not wanted:
        return round(title * 100 * 0.9)  # title alone can say "review", rarely "sure"
    have = [_norm(a) for a in cand.get("authors", [])]
    author = max((_ratio(w, h) for w in wanted for h in have), default=0.0)
    return round((0.7 * title + 0.3 * author) * 100)


def rank(row: dict, cands: list) -> list:
    """Candidates with a `score`, best first, at most MAX_CANDIDATES."""
    lang = row.get("language")
    for c in cands:
        c["score"] = score(row, c)
    cands.sort(key=lambda c: (-c["score"], c.get("language") != lang, not c.get("isbn"), c["olid"]))
    return cands[:MAX_CANDIDATES]


def public(cand: dict) -> dict:
    return {k: v for k, v in cand.items() if k != "isbns"}


def decide(ranked: list) -> dict:
    """Row column updates for a ranked candidate list."""
    top = ranked[0]["score"] if ranked else 0
    base = {"candidates_count": len(ranked), "confidence": top, "error": None}
    if top >= AUTO_MIN:
        best = ranked[0]
        return dict(base, state="matched", olid=best["olid"], auto_picked=True,
                    match_title=(best["title"] or "")[:300] or None,
                    match_author=", ".join(best["authors"])[:200] or None)
    if top >= REVIEW_MIN:
        return dict(base, state="needs_review", olid=None, auto_picked=False)
    return dict(base, state="no_match", olid=None, auto_picked=False, confidence=top)


# ─── matching rows ──────────────────────────────────────────────────────────

def _usable(row: dict) -> bool:
    return bool(row.get("isbn") or row.get("title"))


def _by_title(row: dict, retries: int = RETRIES) -> list:
    params = {"title": row["title"][:200], "limit": MAX_CANDIDATES}
    if row.get("author"):
        params["author"] = row["author"][:200]
    return rank(row, candidates_from(search(params, retries)))


def candidates_for(row: dict) -> list:
    """Live, scored candidates for one row (what the "Change edition" list shows)."""
    cands = []
    if row.get("isbn"):
        cands += candidates_from(search({"q": f"isbn:{row['isbn']}", "limit": MAX_CANDIDATES}, retries=0))
    if row.get("title"):
        cands += _by_title(row, retries=0)
    merged = {c["olid"]: c for c in cands}
    return [public(c) for c in rank(row, list(merged.values()))]


def match_rows(rows: list):
    """Yield (row_id, column updates) as answers arrive, so callers can save in steps."""
    todo, with_isbn = [], [r for r in rows if r.get("isbn")]
    for r in rows:
        if not _usable(r):
            yield r["id"], {"state": "no_match", "candidates_count": 0, "confidence": 0,
                            "error": "No ISBN or title to match on."}
        elif not r.get("isbn"):
            todo.append(r)

    for i in range(0, len(with_isbn), ISBN_BATCH):
        chunk = with_isbn[i:i + ISBN_BATCH]
        docs = search({"q": "isbn:(" + " OR ".join(sorted({r["isbn"] for r in chunk})) + ")", "limit": 100})
        cands = candidates_from(docs)
        for r in chunk:
            hits = rank(r, [dict(c) for c in cands if r["isbn"] in c["isbns"]])
            if hits:
                yield r["id"], decide(hits)
            elif r.get("title"):
                todo.append(r)  # ISBN unknown to Open Library: try the title
            else:
                yield r["id"], decide([])

    for r in todo:
        yield r["id"], decide(_by_title(r))
