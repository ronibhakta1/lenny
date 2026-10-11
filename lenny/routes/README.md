
# Lenny API Routing Documentation

Base URL: `http://localhost:8080/v1/api`

## Endpoints


### 1. Home Page

- **GET /**
  - Renders the index HTML page.
  - **Parameters:** None


### 2. Get Items

- **GET /items**
  - Returns enriched items.
  - **Query Parameters:**
    - `fields` (optional, comma-separated string): Fields to include in response
    - `offset` (optional, int): Pagination offset
    - `limit` (optional, int): Pagination limit
  - Example: `GET /items?fields=title,author&offset=0&limit=10`


### 3. OPDS Feed

- **GET /opds**
  - Returns OPDS feed.
  - Query params:
    - `offset` (int, optional): Items to skip. Paired with `limit`, pages the feed.
    - `limit` (int, optional): Items per page (default 50).
    - `modified_since` (str, optional): ISO 8601 date or timestamp
      (`2026-08-01`, `2026-08-01T12:30:00Z`). Limits the feed to items whose
      `updated_at` is at or after that instant. A value with no timezone is read
      as UTC. Returns `400` if unparseable.
  - Items are ordered oldest-change-first (`updated_at`, then `id`), so paging is
    stable and an incremental consumer can resume where it stopped.
  - The catalog carries a `rel=next` link while more items remain, preserving
    `modified_since`; each publication carries `metadata.modified`.
  - `metadata.numberOfItems` is the size of the whole matching set, not of the
    current page.
  - Example — everything changed since August 1st, 100 at a time:
    ```
    curl "http://localhost:8080/v1/api/opds?modified_since=2026-08-01&limit=100"
    ```
  - **Query Parameters:**
    - `offset` (optional, int): Pagination offset
    - `limit` (optional, int): Pagination limit


### 4. Read Book (Redirect)

- **GET /items/{book_id}/read**
  - Redirects to Thorium Web Reader for the book. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
  - **Query Parameters:**
    - `format` (optional, str, default: "epub"): Book format


### 5. Readium Manifest

- **GET /items/{book_id}/readium/manifest.json**
  - Returns Readium manifest for the book. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
  - **Query Parameters:**
    - `format` (optional, str, default: ".epub"): Book format


### 6. Proxy Readium Requests

- **GET /items/{book_id}/readium/{readium_path}**
  - Proxies requests to Readium server. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
    - `readium_path` (str): Path to resource in Readium
  - **Query Parameters:**
    - `format` (optional, str, default: ".epub"): Book format


### 7. Upload Item

- **POST /upload**
  - Uploads an EPUB file for an OpenLibrary edition (EPUB only; the file must be a real EPUB).
  - **Admin only.** Requires the same credential pair as every `/admin/` route:
    the `X-Admin-Internal-Secret` shared secret (from `auth.env`) *and* an admin
    Bearer token from `POST /admin/auth`. There is no IP-based exemption — a
    caller's network position never authorizes an upload.
  - **Form Data:**
    - `openlibrary_edition` (int, required): OpenLibrary Edition ID (must be positive)
    - `encrypted` (bool, optional, default: false): Set to true if file is encrypted
    - `file` (UploadFile, required): EPUB file (max 100MB)
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/upload" \
      -H "X-Admin-Internal-Secret: $ADMIN_INTERNAL_SECRET" \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      -F "openlibrary_edition=12345678" \
      -F "encrypted=false" \
      -F "file=@book.epub"
    ```


### 8. Authenticate

- **POST /authenticate**
  - Authenticates user via email and OTP. Sets session cookie on success.
  - **Form Data:**
    - `email` (str, required): User email
    - `otp` (str, required): One-time password


### 9. Borrow Item

- **POST /items/{book_id}/borrow**
  - Borrows a book for the authenticated user. Requires session cookie for encrypted books.
  - **Path Parameters:**
    - `book_id` (int): Book identifier
  - **Body Parameters:**
    - `otp` (str, optional): One-time password (if not logged in)
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/items/12345678/borrow" \
      -H "Content-Type: application/json" \
      -d '{"otp": "123456"}'
    ```


### 10. Checkout Multiple Items

- **POST /items/checkout**
  - Checks out multiple books for a user.
  - **Body Parameters (JSON):**
    - `openlibrary_editions` (List[int], required): List of OpenLibrary Edition IDs
    - `email` (str, required): User email
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/items/checkout" \
      -H "Content-Type: application/json" \
      -d '{"openlibrary_editions": [12345678, 23456789], "email": "user@example.com"}'
    ```


### 11. Return Item

- **POST /items/{book_id}/return**
  - Returns a borrowed book for the authenticated user. Requires session cookie.
  - **Path Parameters:**
    - `book_id` (int): Book identifier


### 12. Get Borrowed Items

- **POST /items/borrowed**
  - Returns a list of active borrowed items for the authenticated user. Requires session cookie.


### 13. Logout

- **GET /logout**
  - Logs out the user by deleting the session cookie.
  - **Parameters:** None

---

## Admin Endpoints

All `/admin/*` routes require two headers: `X-Admin-Internal-Secret` and
`Authorization: Bearer <admin token>`. Blocked from external access at the
nginx layer — only reachable from the admin UI calling the API directly on
the internal Docker network.

### Item Management

- **PATCH /admin/items/{book_id}**
  - Updates DRM/loan-duration flags and/or renames an item's OpenLibrary
    edition (fixes a wrong-edition import — moves the underlying S3 files to
    the new key). All fields optional; at least one required.
  - **Body (JSON):**
    - `encrypted` (optional, bool)
    - `loan_duration_days` (optional, int ≥ 0, or `null` to clear the
      override back to the global default). `0` means never expire, same as
      the global setting. A positive value greater than the global max
      (`GET /admin/settings/loan-limits`) is rejected with `400`.
    - `openlibrary_edition` (optional, positive int): renames the item
  - Errors: `400` invalid field, `404` not found, `409` target edition
    already exists, `500` S3/DB error during rename

- **POST /admin/items/{book_id}/reupload**
  - Replaces an item's file (fixes a wrong-file import). Item id and loan
    history are untouched — only the S3 object(s) and `encrypted`/`formats`
    change.
  - **Form Data:** `file` (required, EPUB, max 100MB), `encrypted`
    (optional bool, default false)
  - Response: plain text `"File replaced successfully."`, not JSON.

- **DELETE /admin/items/{book_id}**
  - Removes an item from S3 and the database (loans cascade). `book_id`
    accepts a bare OLID or an edition key (`OL51008637M`).
  - `204` on success, `404` if not found.

- **POST /admin/items/delete**
  - Bulk delete, up to 200 per request.
  - **Body:** `{"book_ids": [...]}` — bare OLIDs or edition keys, mixed OK.
  - Always `200`, with a per-item breakdown so one bad id never masks the
    rest: `{"deleted": [...], "not_found": [...], "failed": {...}, "invalid": [...]}`

### Loan Management

- **GET /admin/loans**
  - Filtered, paginated, sorted loan listing.
  - **Query Parameters:** `limit`, `offset`, `status` (`all` | `active` |
    `returned` | `overdue`), `user` (hex prefix of the patron email hash),
    `sort` (`borrowed_at` | `due_at` | `returned_at`), `order` (`asc` | `desc`)
  - Response: `{"items": [...], "total": <int>, "limit": <int>, "offset": <int>}`

- **POST /admin/loans**
  - Manually grants a loan, reusing the same row-locking/availability/
    idempotency as self-serve borrow. No email is sent — the intended flow is
    to copy the public borrow link
    (`{apiBase}/v1/api/items/{olid}/borrow`) to the patron, who signs in with
    the existing OTP flow, which already grants access to a loan that exists
    for their email.
  - **Body (JSON):** `openlibrary_edition` (int, required), `email` (str, required)
  - `201`: `{"id", "item_id", "openlibrary_edition", "due_date"}`
  - Errors: `400` invalid input / open-access item, `404` item not found,
    `409` no copies available, `403` patron loan limit reached

- **POST /admin/loans/{loan_id}/return**
  - Force-returns a loan by id, no body. `200` on success, `404` if the loan
    doesn't exist.

---

### Bulk Import Batches

State for the bulk EPUB import page (drop a folder, match to Open Library, review,
import). Code lives in `lenny/core/bulk_import/`. EPUB files only.

A batch is **kept for 30 days from its last write** (reads never extend it) and
is then purged, lazily, the next time a batch is created or listed. An expired
batch answers `410`; a purged one `404`. A row is identified by its path, size
and modified time, so re-dropping the same folder re-attaches to the existing
rows instead of duplicating them.

- **POST /admin/import/batches** — create a batch (`201`).
- **GET /admin/import/batches** — unexpired batches, newest first.
- **GET /admin/import/batches/{id}** — summary (also `match`: the newest match job `{status, total, done, error, retry_after}`, or `null`; per process): `total`, `counts` per state, `ready`, `auto_picked`, `skipped`, `encrypted`, `expires_at`.
- **DELETE /admin/import/batches/{id}** — clear it (`204`).
- **PUT /admin/import/batches/{id}/rows** — `{"rows": [{path, size, mtime, isbn?, title?, author?, language?, publisher?, year?, state?: "not_epub"}]}`, at most 500 per call. Idempotent. Returns `{created, existing}`.
- **GET /admin/import/batches/{id}/rows** — `?state=&q=&skipped=&auto_picked=&cursor=&limit=` (limit up to 200). Returns `{rows, next_cursor}`.
- **PATCH /admin/import/batches/{id}/rows/{row_id}** — `{skipped?, olid?, encrypted?}`. A new `olid` on an already imported row moves the stored book to that edition; loans stay with it.
- **POST /admin/import/batches/{id}/actions** — `{action: "skip"|"unskip"|"set_encrypted", value?, state?, q?, skipped?, auto_picked?}` applied to every row matching the filter. Returns `{updated}`.

**Matching and importing** (same admin credentials). Matching runs on one background
thread per process, paced to Open Library's limit (1 request/s, 3/s when
`LENNY_CONTACT_EMAIL` is set), ISBNs fifty to a query. Results are saved to rows as
they arrive, so polling `GET /admin/import/batches/{id}` shows partial progress
and a restart loses nothing (unmatched rows stay `queued`). Matching state is per process, so keep one worker for big imports or Open Library sees one paced stream per worker. Rows the admin has
decided (picked an edition) or skipped are never overwritten. A row without an ISBN is searched by a widening ladder (title + first author, title alone, title without subtitle, then what the file name says), so a long author list or a publisher in the author field does not empty the search; identical rows are looked up once and non-empty answers are cached for 30 minutes (a Re-check bypasses the cache); if Open Library rate limits us with a wait over 20 seconds the job fails at once with `error` and `retry_after` instead of sleeping, and the rows stay `queued` for a retry; titles compare forgivingly (subtitles, `Last, First`, bracketed noise like `(Z-Library)`). A title guessed from the file name alone goes to review, never auto-picked. Rows without an ISBN are looked up ten to a request, a few requests at once (2 in flight, 4 with `LENNY_CONTACT_EMAIL`), and only the rows that batch cannot settle get their own searches. The file's language, publisher and year also count: a different-language edition scores lower, matching publisher/year break ties between editions of the same book. The candidates list adds the editions of the best two works, so the right edition (not just the one search showed) can be picked.

- **POST /admin/import/batches/{id}/match** — `{state?: "queued"|"no_match"|"needs_review", row_ids?: [≤500]}` → `202` job `{id, status: queued|running|done|failed, total, done, error}`. `state: "no_match"` is the *Re-check* button. One active job per batch; asking again returns it.
- **GET /admin/import/match/{job_id}** — job progress (`404` after a restart; use the batch summary then).
- **GET /admin/import/batches/{id}/rows/{row_id}/candidates** — `{candidates: [{olid, title, authors[], year, publisher, language, isbn, score}]}`, best first, live from Open Library. `502` if Open Library is unreachable.
- **GET /admin/import/editions/lookup?key=OL123M** — checks a pasted edition key: `{olid, title, publisher, year, isbn, in_library}`; `400` bad key, `404` unknown to Open Library.
- **POST /admin/import/check-duplicates** — `{olids: [≤1000]}` → `{existing: [...]}` (already in the library).
- **GET /admin/import/limits** — `{max_rows_per_call, isbn_batch, ol_requests_per_second, ol_identified, ol_logged_in, max_file_mb, upload_concurrency, retention_days, formats}`.
- **POST /admin/import/batches/{id}/rows/{row_id}/file** — multipart `file`; imports a matched, unskipped row. The answer is the row; its `state` is the outcome: `imported`, `duplicate` (already in the library), `not_epub`, `failed`. Retrying is safe; a second attempt while one is in progress gets `409` (an abandoned attempt is retaken after 15 minutes). Runs under the same bounded upload pool as other uploads.

Row states: `queued → matched | needs_review | no_match`; `duplicate`; `importing → imported | failed | not_epub`.
`matched` rows carry `olid` and `auto_picked`; `needs_review` rows have no `olid` until the admin picks one.

## Authentication
Most endpoints require a valid session cookie. Use `/authenticate` to obtain one via email and OTP.

## Error Handling
Endpoints return appropriate HTTP status codes and error messages for unauthorized access, invalid input, and server errors.
