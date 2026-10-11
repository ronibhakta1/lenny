"""Match dropped EPUBs to Open Library editions.

Rows with an ISBN go to Open Library fifty at a time (one query for fifty
books). Rows without one, or whose ISBN found nothing, get a ladder of
title/author queries, each tried only if the one before found nothing close:
title + first author, title alone (an author list or a publisher's name can
empty a search), the title without its subtitle, then what the file name says.
Titles and names are compared forgivingly (subtitles, "Last, First" and word
order, bracketed noise like "(Z-Library)"). Every call is paced (limits.Pacer), retried politely on 429, and
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


SUBTITLE_CREDIT = 0.8   # the FILE has a subtitle the record lacks ("Dune: Messiah" vs "Dune"): review, never auto
LESS_SPECIFIC_CREDIT = 0.9  # the RECORD has a subtitle the file lacks ("Digest" vs "Digest: May 17-23"): likely the same series
LONG_AUTHOR_LIST = 3    # a list this long is not a reliable search term


def _clean_title(title) -> str:
    """Drop (bracketed) noise: "(Z-Library)", "[epub]", "(Penguin Classics)"."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", str(title or ""))


def _main_title(title) -> str:
    return _norm(re.split(r"[:\u2013\u2014]| - ", _clean_title(title))[0])


def _has_subtitle(title) -> bool:
    return bool(re.search(r"[:\u2013\u2014]| - ", _clean_title(title)))


def _title_ratio(wanted, have) -> float:
    full = _ratio(_norm(_clean_title(wanted)), _norm(_clean_title(have)))
    credit = SUBTITLE_CREDIT if _has_subtitle(wanted) else LESS_SPECIFIC_CREDIT
    return max(full, credit * _ratio(_main_title(wanted), _main_title(have)))


def _split_names(author) -> list:
    """Names as written, one per person.

    "A; B", "A & B", "A and B" split; "Last, First" (one comma) is flipped; three
    or more comma-separated parts are a list of "First Last" people, as EPUBs of
    anthologies and magazines write them.
    """
    names = []
    for part in re.split(r";|&|\band\b", str(author or "")):
        part = part.strip()
        if part.count(",") >= 2:
            names += [p.strip() for p in part.split(",") if p.strip()]
        elif "," in part:
            last, _, first = part.partition(",")
            names.append(f"{first.strip()} {last.strip()}".strip())
        elif part:
            names.append(part)
    return [n for n in names if _norm(n)]


def _name_ratio(a: str, b: str) -> float:
    """Names compare ignoring word order ("Moskovitz, Diana" is "Diana Moskovitz")."""
    a, b = _norm(a), _norm(b)
    return max(_ratio(a, b), _ratio(" ".join(sorted(a.split())), " ".join(sorted(b.split()))))


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _facts(row: dict) -> tuple:
    """(title, author) to match on: the EPUB's own metadata, else what the file name says."""
    guess = from_path(row.get("path")) if not (row.get("title") and row.get("author")) else {}
    return (row.get("title") or guess.get("title") or "").strip(), row.get("author") or guess.get("author")


def score(row: dict, cand: dict) -> int:
    """0-100: how sure we are that `cand` is the book in `row`."""
    isbn = row.get("isbn")
    if isbn and isbn in cand.get("isbns", ()):
        return 100
    wanted_title, author_text = _facts(row)
    if not wanted_title:
        return 0
    title = _title_ratio(wanted_title, cand.get("title"))
    from_name_only = not row.get("title")  # a guess from the file name never auto-picks
    wanted = _split_names(author_text)
    if not wanted:
        return min(round(title * 100 * 0.9), AUTO_MIN - 1 if from_name_only else 100)  # title alone: review, rarely sure
    # One shared author is strong evidence, so take the best pairing; a long
    # list (a magazine's whole masthead) only needs one of its names to appear.
    author = max((_name_ratio(w, h) for w in wanted for h in cand.get("authors", [])), default=0.0)
    return min(round((0.7 * title + 0.3 * author) * 100), AUTO_MIN - 1 if from_name_only and author < 0.9 else 100)


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
    return bool(row.get("isbn") or row.get("title") or from_path(row.get("path")).get("title"))


_JUNK = re.compile(r"\b(z[- ]?library|libgen|1lib|epub|ebook|retail|vk\.com)\b", re.I)
# A trailing date part of a file name ("9-17-26", "may"); a bare number ("1984") is a title.
_DATEISH = re.compile(r"^\s*(\d{1,2}[-. ]\d{1,2}([-. ]\d{2,4})?|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\s*$", re.I)


def from_path(path: str) -> dict:
    """Best-effort {title, author} from a file name, for books whose own
    metadata is empty or misleading. Understands "author_title_date" (Standard
    Ebooks style), "Title (Author) (site)" and falls back to the whole name."""
    name = re.sub(r"\.epub$", "", str(path or "").rsplit("/", 1)[-1], flags=re.I)
    parens = [p for p in re.findall(r"\(([^)]*)\)", name) if p.strip() and not _JUNK.search(p)]
    base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", " ", name).strip()
    if "_" in base:
        parts = [p for p in base.split("_") if p.strip() and not _DATEISH.match(p)]
        if len(parts) >= 2:
            return {"author": parts[0].replace("-", " ").strip(), "title": parts[1].replace("-", " ").strip()}
        base = " ".join(parts) or base
    out = {"title": " ".join(base.replace("_", " ").split())}
    if parens:
        out["author"] = parens[0].strip()
    return out


def _queries(row: dict) -> list:
    """Search attempts, tightest first. Each is only tried if the one before
    found nothing close, so a clean file costs one request."""
    title, names = (row.get("title") or "").strip(), _split_names(row.get("author"))
    guess = from_path(row.get("path"))
    if not title:
        title, names = guess.get("title", ""), _split_names(guess.get("author"))
    out = []
    def add(t, a=None):
        t = " ".join(_clean_title(t).split())[:200]
        if t:
            q = {"title": t, "limit": MAX_CANDIDATES}
            if a:
                q["author"] = a[:200]
            if q not in out:
                out.append(q)
    primary = names[0] if names and len(names) <= LONG_AUTHOR_LIST else None  # a masthead over-constrains the search
    add(title, primary)
    add(title)                                   # the author may be a publisher or spelled differently
    add(re.split(r"[:\u2013\u2014]| - ", _clean_title(title))[0])   # without a subtitle
    if guess.get("title"):
        add(guess["title"], guess.get("author"))
        add(guess["title"])
    return out


def _by_title(row: dict, retries: int = RETRIES, exhaustive: bool = False) -> list:
    """Ranked candidates for a row without a (known) ISBN, widening the search
    until something scores at least REVIEW_MIN (or, if exhaustive, trying all)."""
    pool = {}
    for params in _queries(row):
        for c in candidates_from(search(params, retries)):
            pool.setdefault(c["olid"], c)
        ranked = rank(row, [dict(c) for c in pool.values()])
        if ranked and ranked[0]["score"] >= REVIEW_MIN and not exhaustive:
            return ranked
    return rank(row, [dict(c) for c in pool.values()])


def candidates_for(row: dict) -> list:
    """Live, scored candidates for one row (what the "Change edition" list shows):
    every query is tried, so the admin sees the widest sensible list."""
    cands = []
    if row.get("isbn"):
        cands += candidates_from(search({"q": f"isbn:{row['isbn']}", "limit": MAX_CANDIDATES}, retries=0))
    if row.get("title") or row.get("path"):
        cands += _by_title(row, retries=0, exhaustive=True)
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
            elif _usable(r):
                todo.append(r)  # ISBN unknown to Open Library: try the title
            else:
                yield r["id"], decide([])

    for r in todo:
        yield r["id"], decide(_by_title(r))
