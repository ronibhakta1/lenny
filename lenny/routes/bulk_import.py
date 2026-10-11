#!/usr/bin/env python

"""
Admin endpoints for bulk-import batches (see lenny/core/bulk_import/batches.py).

    POST   /admin/import/batches
    GET    /admin/import/batches
    GET    /admin/import/batches/{id}
    DELETE /admin/import/batches/{id}
    PUT    /admin/import/batches/{id}/rows
    GET    /admin/import/batches/{id}/rows
    PATCH  /admin/import/batches/{id}/rows/{row_id}
    DELETE /admin/import/batches/{id}/rows/{row_id}
    PUT    /admin/import/batches/{id}/rows/{row_id}/staged   keep the file on the server
    POST   /admin/import/batches/{id}/actions

Every route is admin-only, same credential pair as the other /admin/ routes.
Database work runs on a worker thread and drops that thread's session after,
since the app's per-request cleanup only reaches the event loop's own thread.
"""

import anyio
from typing import Optional

from fastapi import APIRouter, Body, File, HTTPException, Request, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

from lenny.core.bulk_import import batches, staging
from lenny.core.db import session as db
from lenny.core.exceptions import (
    DatabaseUpdateError, FileTooLargeError, InvalidFileError, ItemExistsError, ItemNotFoundError, S3UploadError)
from lenny.routes.api import _UPLOAD_LIMITER, _require_admin

router = APIRouter()


async def _run(fn, *args, _limiter=None, **kwargs):
    def call():
        try:
            return fn(*args, **kwargs)
        except batches.ImportBatchNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except batches.ImportBatchExpired as e:
            raise HTTPException(status_code=410, detail=str(e))
        except batches.ImportRowNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (batches.ImportRowBusy, ItemExistsError) as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ItemNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except FileTooLargeError as e:
            raise HTTPException(status_code=413, detail=str(e))
        except InvalidFileError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except (S3UploadError, DatabaseUpdateError):
            raise HTTPException(status_code=500, detail="Internal server error")
        finally:
            db.remove()
    if _limiter is not None:
        return await anyio.to_thread.run_sync(call, limiter=_limiter)
    return await run_in_threadpool(call)


def _bool(value, name):
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"'{name}' must be a boolean")
    return value


@router.post("/admin/import/batches", status_code=status.HTTP_201_CREATED)
async def create_batch(request: Request):
    _require_admin(request)
    return await _run(batches.create_batch)


@router.get("/admin/import/batches")
async def list_batches(request: Request):
    _require_admin(request)
    return {"batches": await _run(batches.list_batches)}


@router.get("/admin/import/batches/{batch_id}")
async def get_batch(request: Request, batch_id: int):
    _require_admin(request)
    return await _run(batches.get_summary, batch_id)


@router.delete("/admin/import/batches/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_batch(request: Request, batch_id: int):
    _require_admin(request)
    await _run(batches.delete_batch, batch_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/admin/import/batches/{batch_id}/rows")
async def put_rows(request: Request, batch_id: int, body: dict = Body(...)):
    _require_admin(request)
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise HTTPException(status_code=400, detail="'rows' must be a list")
    return await _run(batches.upsert_rows, batch_id, rows)


@router.get("/admin/import/batches/{batch_id}/rows")
async def get_rows(
    request: Request, batch_id: int,
    state: Optional[str] = None, q: Optional[str] = None,
    cursor: Optional[int] = None, limit: int = 100,
    skipped: Optional[bool] = None, auto_picked: Optional[bool] = None,
):
    _require_admin(request)
    if state is not None and state not in batches.STATES:
        raise HTTPException(status_code=400, detail=f"'state' must be one of {', '.join(batches.STATES)}")
    return await _run(
        batches.list_rows, batch_id, state=state, q=q, cursor=cursor, limit=limit,
        skipped=skipped, auto_picked=auto_picked)


@router.patch("/admin/import/batches/{batch_id}/rows/{row_id}")
async def patch_row(request: Request, batch_id: int, row_id: int, body: dict = Body(...)):
    _require_admin(request)
    olid = body.get("olid")
    if olid is not None and (isinstance(olid, bool) or not isinstance(olid, int) or not 0 < olid < 2 ** 31):
        raise HTTPException(status_code=400, detail="'olid' must be a positive integer")
    try:
        skipped, encrypted = _bool(body.get("skipped"), "skipped"), _bool(body.get("encrypted"), "encrypted")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if olid is None and skipped is None and encrypted is None:
        raise HTTPException(status_code=400, detail="Provide skipped, olid and/or encrypted")
    return await _run(batches.update_row, batch_id, row_id, skipped=skipped, olid=olid, encrypted=encrypted)


@router.delete("/admin/import/batches/{batch_id}/rows/{row_id}")
async def delete_row(request: Request, batch_id: int, row_id: int):
    """Remove a row from the batch; returns the updated batch summary. 404 if the
    row is gone, 409 while it is being imported. An imported book stays in the library."""
    _require_admin(request)
    return await _run(batches.delete_row, batch_id, row_id)


@router.put("/admin/import/batches/{batch_id}/rows/{row_id}/staged")
async def stage_file(request: Request, batch_id: int, row_id: int,
                     file: UploadFile = File(..., description="The row's EPUB file")):
    """Keep this book's file on the server so importing it later needs no browser.
    Idempotent (a second upload replaces the first). Runs under the same bounded
    upload pool as other uploads. Returns the row, with `staged: true`."""
    _require_admin(request)
    return await _run(staging.stage, batch_id, row_id, file, _limiter=_UPLOAD_LIMITER)


@router.post("/admin/import/batches/{batch_id}/actions")
async def post_action(request: Request, batch_id: int, body: dict = Body(...)):
    _require_admin(request)
    state = body.get("state")
    if state is not None and state not in batches.STATES:
        raise HTTPException(status_code=400, detail=f"'state' must be one of {', '.join(batches.STATES)}")
    try:
        skipped, auto_picked = _bool(body.get("skipped"), "skipped"), _bool(body.get("auto_picked"), "auto_picked")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    updated = await _run(
        batches.bulk_update, batch_id, body.get("action"), state=state, q=body.get("q"),
        value=body.get("value"), skipped=skipped, auto_picked=auto_picked)
    return {"updated": updated}
