# The borrow seam, end to end

```bash
tests/e2e/run_borrow_e2e.sh --openlibrary /path/to/an/openlibrary/checkout
```

One command. It brings up its own node, walks a patron from "I have never
signed in" to "Open Library holds my loan", and tears everything down again.

**What it is for.** Before this, the Open Library half of the Lenny borrow flow
had only ever worked in a running Docker container beside a database somebody
had made by hand — a thing that dies overnight and takes the division's only
evidence with it. The exit code is the point: it is the one instrument that can
see the seam between the two systems, and a seam belongs to whoever owns the
outcome, not to whoever owns either side's code.

## Exit codes

| code | meaning |
|------|---------|
| `0` | it ran, and the seam works |
| `1` | it ran, and **the seam is broken** — read the red step |
| `2` | it **could not run** (no Docker, an image missing, no usable Open Library checkout). This is not evidence about the seam in either direction. |

Keeping `2` apart from `1` is deliberate. "I could not check" and "I checked and
it is broken" get confused constantly, and the confusion always resolves in the
reassuring direction.

## What you need

* Docker (Colima is fine). The script never builds an image.
* `lenny-api:latest` — the node. Override with `LENNY_IMAGE`.
* `oldev:latest` — Open Library's dev image. Override with `OL_IMAGE`.
* `postgres:16`. Override with `PG_IMAGE`.
* An Open Library checkout carrying the Lenny borrow code: it must have
  `openlibrary/core/provider_tokens.py` and a `mediated_borrow()` or `borrow()`
  in `openlibrary/plugins/upstream/lenny.py`. Pass it with `--openlibrary`, or
  set `OL_CHECKOUT`. Today that means a branch descended from openlibrary#13552
  or #13687; `master` will make the script exit `2`.

nginx is bypassed on purpose. Its `lenny_reader` / `lenny_admin` upstreams
cannot be built on a machine without `buildx`, and nothing in this flow needs
them — the api image runs uvicorn directly.

## Options

| flag | effect |
|------|--------|
| `--openlibrary PATH` | the Open Library checkout to exercise |
| `--keep` | leave the containers up for poking at; prints the command to remove them |
| `--selftest` | prove the reporting guard fires. No Docker, no network. |
| `--break MODE` | deliberately break one thing; see below |

## What each step proves

1–5 (driver) bring up a throwaway node: network, Postgres, alembic-migrated
schema, the OTP stub, an operator-registered OAuth client, and two lendable
items. 6–12 walk Lenny's side: the patron's OTP sign-in, RFC 8414 discovery,
PKCE S256 authorization, the consent screen, the code→token exchange, a borrow
and the patron's loans. 13–20 are Open Library's own code: the bootstrap, the
two tables, `node_for_edition`, `ProviderToken.upsert`, a borrow through
`lenny.borrow()`/`mediated_borrow()`, a forced refresh through
`node_refresher()`, `provider_loans()`, and finally `datetime_from_isoformat()`
on the expiry that comes back — the last hop before the loans page renders.

Every row prints the value it rests on. A row with no value prints `FAIL`, not a
blank, and fails the run; `--selftest` watches that guard fire rather than
asserting it does.

## What it does NOT prove

* **It does not run Open Library's web app.** No request context, no templates,
  no infogami app. It exercises the functions behind the pages, not the pages.
* **It does not prove a deployed Open Library has `provider_tokens`.** That
  table is not in `openlibrary/core/schema.sql` on any branch shipped so far; it
  exists only as a constant in `openlibrary/tests/core/test_provider_tokens.py`,
  which is where this harness reads it from. See openlibrary#13689. This is
  precisely the fixture-only-table shape, and a green run here says nothing
  about it.
* **It does not exercise Open Library's real OTP service.** `OTP_SERVER` points
  at `tests/oauth2/otp_stub.py`, which implements only the success and mismatch
  answers. Rate limiting, stale credentials and `auth_service_unavailable` are
  not covered here; `tests/test_otp_error_surfacing.py` covers them.
* **It does not exercise nginx, the reader, S3 or Readium.** Nothing reads the
  book. The borrow is a loan row, not a download.
* **It does not test a multi-node merge.** One node, one patron.

## The deliberate-failure drill

A harness nobody has watched fail is not an instrument. Both modes name the step
they should turn red; a run that goes red somewhere else is itself a finding.

```bash
tests/e2e/run_borrow_e2e.sh --openlibrary PATH --break issuer
tests/e2e/run_borrow_e2e.sh --openlibrary PATH --break advertised-issuer
```

* `--break issuer` configures Open Library with a node issuer that does not
  resolve. Expect green through step 16 and red at step 17, the first call that
  leaves the process: `lenny.borrow() could not reach the node`.
* `--break advertised-issuer` makes the node advertise a host nothing can
  resolve while Open Library is configured with the one that works. Expect red
  at step 8, discovery. This is the hazard below, caught earlier than production
  would catch it.

Under `--break`, a run in which everything passes exits `1` and says so: the
harness could not see the failure it was told to produce.

## Facts this harness encodes, so nobody has to rediscover them

Each of these cost somebody hours. Where one has since been measured again by
this harness, that is noted.

* **`node_refresher` takes the node *dict*, not the provider name.** A string
  raises `TypeError: string indices must be integers` from inside `lenny.py`,
  which reads like a library bug.
* **`node_refresher` follows *discovery's* `token_endpoint`; `provider_loans`
  builds from the *configured* issuer.** If the node advertises a host Open
  Library cannot resolve, loans keep working and refresh fails — and
  `get_fresh` **deletes** the grant on failure rather than retrying. `LENNY_HOST`
  is therefore set to the api container's own name, so the node advertises a
  host the Open Library container can resolve. `--break advertised-issuer`
  reproduces the mismatch.
* **The session cookie is minted `secure=True`, hardcoded.** Over plain http a
  client that stores cookies normally will never send it back. Every request
  after sign-in carries an explicit `Cookie:` header instead.
* **The cookie is bound to the client IP that minted it.** That is why both
  halves run in one process in one container; two processes on two addresses
  produce a redirect to the login screen that reads like a broken consent page.
* **A borrowable item must have `encrypted=True`.** An open-access item answers
  `not_lendable`, which reads like a broken borrow and is not one. `formats` is
  an uppercase enum: `EPUB`, `PDF`, `EPUB_PDF`.
* **Lending must be mode `ol` with both `OL_S3_ACCESS_KEY` and
  `OL_S3_SECRET_KEY` non-empty**, or every OTP call answers
  `lending_not_configured`. Those two are read at import, so on a long-lived
  container a change needs a *recreate*, not a restart. This harness always
  creates a fresh container, so the trap cannot bite here.
* **`redirect_uri` must be `https://`, `http://` on loopback, or a private-use
  scheme** (RFC 8252). `http://openlibrary.example/...` is refused at
  registration with a message that does not obviously say why.
* **Open Library's `datetime_from_isoformat` cannot parse a timezone-aware
  string at all** — a trailing `Z` or `+00:00` raises `ValueError`, and a
  negative offset raises `TypeError`, so a caller catching only `ValueError`
  still dies. The node's `due_at` *is* timezone-aware;
  `lenny._expiry` normalises it. Step 20 checks both directions: the normalised
  value parses, and the node's raw value still does not.
* **`provider_loans` never raises, and returns empty when `lenny_nodes` is
  unset** — without ever touching the database. An empty result is not evidence
  the node is healthy.
* **`web.config.debug` is `True` by default in `oldev:latest`**, and web.py then
  echoes every statement — including the encrypted token in the
  `provider_tokens` INSERT. The harness sets it `False` before touching the DB.
* **A Docker bind mount from a path the VM does not mount fails silently.**
  Colima mounts `/Users/<you>` only; a `/private/tmp` source is *created* empty
  inside the VM and the container reads nothing, with no error. Everything this
  harness mounts lives inside the checkouts.

## Leaving nothing behind

Every container, network and database is named `lennye2e_<runid>_*`, where the
run id is fresh each run. Cleanup fires on success, on failure and on Ctrl-C,
removes only names carrying that run's own id, and then counts what survived —
so it can say `nothing of this run is left running` and mean it. It never
touches a container it did not create, so it is safe to run beside a hand-made
stack.
