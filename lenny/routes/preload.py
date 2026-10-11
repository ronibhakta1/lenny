#!/usr/bin/env python

"""
Admin endpoints for preloading free books (see lenny/core/preload/).

    GET  /admin/preload/catalog      ?q=&collection=&hide_owned=&sort=&new_only=&cursor=&limit=
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
