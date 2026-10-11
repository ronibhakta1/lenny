#!/usr/bin/env python

"""
Admin endpoints for preloading free books (see lenny/core/preload/).

    GET  /admin/preload/catalog      ?q=&collection=&hide_owned=&sort=&new_only=&cursor=&limit=
    GET  /admin/preload/books/{olid}
    POST /admin/preload/refresh
    GET  /admin/preload/collections
    POST /admin/preload/import       {olids?, source_ids?, collection?}

Progress of a load shows up per book in GET /admin/imports.
"""

import httpx
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request, status

from lenny.core.preload import catalog, service
from lenny.routes.api import _require_admin

router = APIRouter()


def _catalog_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except catalog.CatalogUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Plain `def`: the first call builds the catalog over the network, which must not
# run on the event loop.
@router.get("/admin/preload/catalog")
def get_catalog(request: Request, q: str | None = None, collection: str | None = None,
                hide_owned: bool = False, cursor: int = 0, limit: int = catalog.DEFAULT_PAGE,
                sort: str = "title", new_only: bool = False):
    _require_admin(request)
    cat = _catalog_call(catalog.get)
    owned = service.existing_among(b["olid"] for b in cat["books"])
    result = _catalog_call(catalog.search, q, collection, cursor, limit, hide=owned if hide_owned else None,
                          sort=sort, new_only=new_only)
    for book in result["books"]:
        book["in_library"] = book["olid"] in owned
    result["in_library"] = len(owned)
    return result


@router.post("/admin/preload/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh_catalog(request: Request):
    """Look at archive.org again now. The list also follows the archive.org item by itself (about a
    minute after books are added there); this is for the impatient. The old list stays visible
    until the new one is ready; `refreshing` in the catalog response says when it is."""
    _require_admin(request)
    return {"started": catalog.refresh()}


@router.get("/admin/preload/books/{olid}")
def get_book(request: Request, olid: int):
    """Everything about one catalog book, for a details sheet: the catalog fields plus publisher,
    publish date, pages, ISBN, languages, description and all subjects from Open Library, and links
    to its Open Library, Standard Ebooks and archive.org pages. `details_loaded: false` means Open
    Library did not answer and only the catalog fields are present. `404` if it is not in the catalog."""
    _require_admin(request)
    book = _catalog_call(catalog.details, olid)
    if book is None:
        raise HTTPException(status_code=404, detail="That book is not in the free-books catalog.")
    book["in_library"] = bool(service.existing_among([olid]))
    book["is_new"] = catalog.is_new(book)
    return book


@router.get("/admin/items/{olid}/metadata")
def get_item_metadata(request: Request, olid: int):
    """Details for a book that is in the library (any book, not only the free ones): publisher, publish
    date, pages, ISBN, languages, description, all subjects and links, in the same shape as
    GET /admin/preload/books/{olid}. For a book that is not a Standard Ebooks title `source_id`, `size`,
    `added_at` and the Standard Ebooks / archive.org links are null. `details_loaded: false` means Open
    Library did not answer (the sheet can offer a retry). `404` if the book is not in the library."""
    _require_admin(request)
    if not service.existing_among([olid]):
        raise HTTPException(status_code=404, detail="That book is not in your library.")
    return catalog.book_metadata(olid)


@router.get("/admin/preload/collections")
def get_collections(request: Request):
    _require_admin(request)
    cat = _catalog_call(catalog.get)
    owned = service.existing_among(b["olid"] for b in cat["books"])
    return {"collections": catalog.collections(owned)}


@router.post("/admin/preload/import", status_code=status.HTTP_202_ACCEPTED)
def import_books(request: Request, background_tasks: BackgroundTasks, body: dict = Body(...)):
    """Load a pick: any of `olids` (ints), `source_ids` (Standard Ebooks ids, e.g. a pasted
    list) and `collection`. Books already in the library are skipped before downloading."""
    _require_admin(request)
    olids, source_ids, collection = body.get("olids", []), body.get("source_ids", []), body.get("collection")
    if (not isinstance(olids, list) or not all(isinstance(o, int) and not isinstance(o, bool) for o in olids)
            or not isinstance(source_ids, list) or not all(isinstance(s, str) for s in source_ids)
            or not (collection is None or isinstance(collection, str))):
        raise HTTPException(status_code=400, detail="'olids' must be integers, 'source_ids' strings, 'collection' a string")
    if not (olids or source_ids or collection):
        raise HTTPException(status_code=400, detail="Pick some books: olids, source_ids or a collection")
    if service.running():
        raise HTTPException(status_code=409, detail="A preload is already running; wait for it to finish.")
    plan = _catalog_call(service.plan, olids, [s.strip() for s in source_ids], collection)
    if plan["to_load"]:
        background_tasks.add_task(service.load, plan["to_load"])
    return {"queued": len(plan["to_load"]), "already_in_library": plan["already_in_library"],
            "unknown": plan["unknown"], "size": sum(b["size"] for b in plan["to_load"])}
