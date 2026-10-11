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
    POST   /admin/import/batches/{id}/match           start matching (queued rows, or state=no_match to re-check)
    GET    /admin/import/match/{job_id}               progress of a match job
    GET    /admin/import/batches/{id}/rows/{row}/candidates
    GET    /admin/import/batches/{id}/rows/{row}/search?q=    find an edition by words, ISBN or OL key
    POST   /admin/import/batches/{id}/rows/{row}/file import one matched row (multipart `file`)
    POST   /admin/import/batches/{id}/rows/{row}/import import a row whose file was staged (no upload)
    GET    /admin/import/editions/lookup?key=OL123M   check a pasted edition key
    POST   /admin/import/check-duplicates             which editions are already in the library
    GET    /admin/import/limits

Every route is admin-only, same credential pair as the other /admin/ routes.
Database work runs on a worker thread and drops that thread's session after,
since the app's per-request cleanup only reaches the event loop's own thread.
"""

import re
from typing import Optional

import anyio
from fastapi import APIRouter, Body, File, HTTPException, Request, Response, UploadFile, status
from starlette.concurrency import run_in_threadpool

from lenny.core.bulk_import import batches, import_job, importer, jobs, limits, matcher, staging
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
        except matcher.UpstreamError as e:
            raise HTTPException(status_code=502, detail=str(e))
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
    summary = await _run(batches.get_summary, batch_id)
    summary["match"] = jobs.latest(batch_id)   # newest match job, or None (this process only)
    summary["import"] = import_job.latest(batch_id)   # newest import job, or None
    return summary


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
    cursor: Optional[int] = None, limit: int = 100, offset: Optional[int] = None, total: bool = False,
    skipped: Optional[bool] = None, auto_picked: Optional[bool] = None, staged: Optional[bool] = None,
    exclude_state: Optional[str] = None, order: str = "id",
):
    _require_admin(request)
    if state is not None and state not in batches.STATES:
        raise HTTPException(status_code=400, detail=f"'state' must be one of {', '.join(batches.STATES)}")
    return await _run(
        batches.list_rows, batch_id, state=state, q=q, cursor=cursor, limit=limit,
        skipped=skipped, auto_picked=auto_picked, staged=staged, offset=offset, with_total=total,
        exclude_state=exclude_state, order=order)


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
    def work():
        row = batches.get_row(batch_id, row_id)          # 404/410 before anything is dropped
        summary = batches.delete_row(batch_id, row_id)
        matcher.forget(row)                              # nothing cached may answer for it again
        return summary
    return await _run(work)


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
        value=body.get("value"), skipped=skipped, auto_picked=auto_picked, row_ids=body.get("row_ids"),
        exclude_state=body.get("exclude_state"))
    if body.get("action") == "remove":
        matcher.clear_cache()                            # many rows went; cheaper than finding each one's entries
    return {"updated": updated}


# ─── matching ────────────────────────────────────────────────────────────────

@router.post("/admin/import/batches/{batch_id}/match", status_code=status.HTTP_202_ACCEPTED)
async def start_match(request: Request, batch_id: int, body: dict = Body(default={})):
    """Queue matching. Body (all optional): `state` queued (default) | no_match
    (the Re-check button) | needs_review, and `row_ids` (at most 500) to narrow it.
    Returns the job; poll it, or the batch summary, for progress."""
    _require_admin(request)
    return await _run(jobs.start, batch_id, body.get("state") or batches.QUEUED, body.get("row_ids"))


@router.post("/admin/import/batches/{batch_id}/import", status_code=status.HTTP_202_ACCEPTED)
async def start_import(request: Request, batch_id: int, body: dict = Body(default={})):
    """Import every ready row whose file is on the server (see PUT .../staged), in the
    background; closing the page does not stop it. Body (optional): `retry_failed: true`
    to also retry rows that failed. Returns the job `{id, status, total, done, imported,
    duplicate, failed, waiting, error}`: `waiting` counts ready rows whose file has not
    reached the server yet (they import on a later run). Poll the batch summary (`import`,
    `counts`) or GET /admin/import/jobs/{id}. One active job per batch."""
    _require_admin(request)
    retry = body.get("retry_failed", False)
    if not isinstance(retry, bool):
        raise HTTPException(status_code=400, detail="'retry_failed' must be a boolean")
    return await _run(import_job.start, batch_id, retry)


@router.get("/admin/import/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    """Progress of a match or an import job. 404 after a restart; the batch summary says the same."""
    _require_admin(request)
    job = jobs.get(job_id) or import_job.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return job


@router.get("/admin/import/match/{job_id}")
async def get_match(request: Request, job_id: str):
    """Job progress. 404 after a restart or many newer jobs; the batch summary's
    counts (queued vs matched) say the same thing and survive both."""
    _require_admin(request)
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown match job.")
    return job


@router.get("/admin/import/batches/{batch_id}/rows/{row_id}/candidates")
async def row_candidates(request: Request, batch_id: int, row_id: int):
    _require_admin(request)
    def work():
        found = matcher.candidates_for(batches.get_row(batch_id, row_id))
        return {"candidates": _mark_owned(found)}
    return await _run(work)


def _mark_owned(candidates: list) -> list:
    """Say which candidates are already in the library, so the page can show it."""
    owned = set(batches.existing_olids([c["olid"] for c in candidates]))
    return [dict(c, in_library=c["olid"] in owned) for c in candidates]


@router.get("/admin/import/batches/{batch_id}/rows/{row_id}/search")
async def search_row_editions(request: Request, batch_id: int, row_id: int, q: str = ""):
    """The "find it yourself" box: `q` is a title and/or author, an ISBN, or an Open
    Library edition key or link. Returns {candidates: [...]} in the candidates shape
    plus `in_library`, best match to this row first. `400` if `q` is under 2 characters,
    `502` if Open Library is unreachable."""
    _require_admin(request)
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="Type at least 2 characters.")
    def work():
        row = batches.get_row(batch_id, row_id)
        return {"candidates": _mark_owned(matcher.search_books(q, row))}
    return await _run(work)


_KEY = re.compile(r"^(?:/?books/)?OL(\d{1,10})M$", re.I)


@router.get("/admin/import/editions/lookup")
async def lookup_edition(request: Request, key: str):
    """Check an edition key the admin pasted (`OL123M` or `/books/OL123M`)."""
    _require_admin(request)
    m = _KEY.match(key.strip())
    if not m:
        raise HTTPException(status_code=400, detail="Not an Open Library edition key (like OL123456M).")
    olid = int(m.group(1))
    def work():
        edition = matcher.lookup_edition(olid)
        if edition is None:
            raise batches.ImportRowNotFound(f"Open Library has no edition OL{olid}M.")
        return dict(edition, in_library=bool(batches.existing_olids([olid])))
    return await _run(work)


@router.post("/admin/import/check-duplicates")
async def check_duplicates(request: Request, body: dict = Body(...)):
    _require_admin(request)
    olids = body.get("olids")
    if (not isinstance(olids, list) or len(olids) > limits.MAX_OLIDS_PER_CHECK
            or not all(isinstance(o, int) and not isinstance(o, bool) and 0 < o < 2 ** 31 for o in olids)):
        raise HTTPException(status_code=400, detail=f"'olids' must be a list of at most {limits.MAX_OLIDS_PER_CHECK} edition numbers")
    return {"existing": await _run(batches.existing_olids, olids)}


@router.get("/admin/import/limits")
async def get_limits(request: Request):
    _require_admin(request)
    return await _run(limits.limits)


@router.post("/admin/import/batches/{batch_id}/rows/{row_id}/import")
async def import_staged_file(request: Request, batch_id: int, row_id: int):
    """Import a row whose file was sent with PUT .../staged: finished on the server,
    nothing to upload. The row's `state` in the answer is the outcome, as for `/file`
    (imported, duplicate, failed). `400` if the file was never staged."""
    _require_admin(request)
    return await _run(importer.import_staged, batch_id, row_id, _limiter=_UPLOAD_LIMITER)


@router.post("/admin/import/batches/{batch_id}/rows/{row_id}/file")
async def import_file(request: Request, batch_id: int, row_id: int,
                      file: UploadFile = File(..., description="The row's EPUB file")):
    """Store one matched book. The row's `state` in the answer is the outcome:
    imported, duplicate (already in the library), not_epub or failed."""
    _require_admin(request)
    return await _run(importer.import_row, batch_id, row_id, file, _limiter=_UPLOAD_LIMITER)
