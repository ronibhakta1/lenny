#!/usr/bin/env python

"""
Links an admin can copy and share for this Lenny library.

    GET /admin/library/links

The OPDS feed (what reading apps subscribe to), a per-book OPDS address, and the
address that opens the feed in Internet Archive's Book Server reader
(reader.archive.org). They are built from the address people outside use to reach
this Lenny: `LENNY_PUBLIC_URL` if set, else the address the admin's own request came in on.
"""

import ipaddress
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request

from lenny import configs
from lenny.routes.api import _require_admin

router = APIRouter()

READER = "https://reader.archive.org/catalog?home="
OPDS_PATH = "/v1/api/opds"


def _is_private(host: str) -> bool:
    """True for an address only this machine or its network can reach (localhost, 192.168.x.x, .local)."""
    host = (host or "").split(":")[0].strip("[]").lower()
    if host in ("", "localhost") or host.endswith((".local", ".internal", ".lan")) or "." not in host and ":" not in host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _base(request: Request) -> tuple:
    """(base url, where it came from). Configured address first; else the forwarded or direct host."""
    if configs.PUBLIC_URL:
        return configs.PUBLIC_URL, "configured"
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    if proto not in ("http", "https") or not host or any(c in host for c in " /\\@?#"):
        host, proto = "localhost", "http"
    return f"{proto}://{host}", "request"


@router.get("/admin/library/links")
async def library_links(request: Request):
    """Copyable links for this library: `opds_feed`, `opds_book_template` (replace `{olid}` with a book's
    edition number), `reader_url` (the feed opened in Internet Archive's Book Server reader), plus `base_url`,
    `source` (configured or request) and `is_public`. When `is_public` is false the address only works on
    this network (a tunnel or domain must be set with LENNY_PUBLIC_URL for others to use it)."""
    _require_admin(request)
    base, source = _base(request)
    feed = f"{base}{OPDS_PATH}"
    return {
        "base_url": base, "source": source, "is_public": not _is_private(urlsplit(base).netloc),
        "opds_feed": feed, "opds_search": f"{feed}/search?query={{query}}", "opds_book_template": f"{feed}/{{olid}}",
        "reader_url": f"{READER}{quote(feed, safe='')}",
    }
