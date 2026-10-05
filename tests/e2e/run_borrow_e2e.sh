#!/usr/bin/env bash
#
# Walk the Lenny <-> Open Library borrow seam end to end, against a real node.
#
#     tests/e2e/run_borrow_e2e.sh --openlibrary /path/to/an/openlibrary/checkout
#
# Exit codes, and the distinction is the whole point:
#
#     0   ran, and the seam works
#     1   ran, and the seam is BROKEN  (a real failure; read the step that said FAIL)
#     2   could not run  (Docker absent, an image missing, no usable Open Library
#         checkout) -- this is NOT evidence about the seam either way
#
# Everything it creates is named `lennye2e_<runid>_*` and is torn down on exit,
# including on failure and on Ctrl-C. It never touches a container it did not
# create, so it is safe to run beside a hand-made stack.
#
# See tests/e2e/README.md for what each step proves and what it does not.

set -uo pipefail

EXIT_OK=0
EXIT_BROKEN=1
EXIT_CANNOT_RUN=2

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
PREFIX="lennye2e_${RUN_ID}"

NET="${PREFIX}_net"
DB="${PREFIX}_db"
API="${PREFIX}_api"
OL="${PREFIX}_ol"

# Bound INSIDE the api container only: the stub is started with `docker exec`
# in that container's own network namespace, so nothing outside it can reach
# the stub and no host port is taken. That is also why otp_stub.py's hardcoded
# 127.0.0.1 bind needs no change.
OTP_PORT=18311
OTP_CODE=123456

LENNY_PORT_IN=1337
PATRON_EMAIL="e2e-patron@example.org"
# Two books: one the protocol borrows directly, one Open Library borrows through
# its own code. Each item has a single copy, so reusing one edition would make
# the second borrow answer `unavailable` -- a true answer to the wrong question.
EDITION_ID=51008637
OL_EDITION_ID=51008638
PROVIDER_NAME="lenny"

DB_USER=librarian
DB_PASSWORD="e2e-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
DB_NAME=lenny
OL_DB_NAME=openlibrary_e2e
LENNY_SEED="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"

LENNY_IMAGE="${LENNY_IMAGE:-lenny-api:latest}"
OL_IMAGE="${OL_IMAGE:-oldev:latest}"
PG_IMAGE="${PG_IMAGE:-postgres:16}"

OL_CHECKOUT="${OL_CHECKOUT:-}"
KEEP=0
SELFTEST=0
BREAK_STEP=""

while [ $# -gt 0 ]; do
  case "$1" in
    --openlibrary) OL_CHECKOUT="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --selftest) SELFTEST=1; shift ;;
    --break) BREAK_STEP="$2"; shift 2 ;;   # see README: deliberate-failure drill
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit "$EXIT_CANNOT_RUN" ;;
  esac
done

# ── output ───────────────────────────────────────────────────────────────────
# Every row carries the value it rests on. A row with nothing after the arrow
# is a broken harness, not a pass, and `step_value` refuses to print one.

STEP_N=0
FAILED=0

_c() { printf '\033[%sm%s\033[0m' "$1" "$2"; }

step() {
  STEP_N=$((STEP_N + 1))
  printf '\n%s %s\n' "$(_c '1;36' "── step ${STEP_N}")" "$(_c 1 "$1")"
}

step_value() {  # step_value <label> <value>
  local label="$1" value="${2-}"
  if [ -z "${value//[[:space:]]/}" ]; then
    printf '   %s %s -> %s\n' "$(_c '31' 'FAIL')" "$label" \
      "$(_c '31' '<empty> -- the harness produced no value here; this is not a pass')"
    FAILED=1
    return 1
  fi
  printf '   %s %s -> %s\n' "$(_c '32' 'ok  ')" "$label" "$value"
}

step_fail() {
  printf '   %s %s\n' "$(_c '31' 'FAIL')" "$1"
  FAILED=1
}

note() { printf '        %s\n' "$(_c '90' "$1")"; }

cannot_run() {
  printf '\n%s %s\n' "$(_c '1;33' 'SKIP')" "$1"
  printf '%s\n' "$(_c '90' 'Exiting 2: could not run. This says nothing about whether the seam works.')"
  exit "$EXIT_CANNOT_RUN"
}

# ── selftest ─────────────────────────────────────────────────────────────────
# The one failure this harness cannot afford is a row that carries no value,
# because it reads exactly like a pass. So the guard is watched failing rather
# than asserted. Needs no Docker and no network.

if [ "$SELFTEST" = 1 ]; then
  rc=0
  printf '%s\n' "$(_c 1 'selftest: a row with no value must not read as a pass')"
  for empty in "" "   "; do
    FAILED=0
    out="$(step_value "deliberately empty" "$empty")"
    printf '%s\n' "$out"
    case "$out" in *FAIL*) ;; *) printf '   SELFTEST FAILED: empty row did not print FAIL\n'; rc=1 ;; esac
    step_value "deliberately empty" "$empty" >/dev/null && { printf '   SELFTEST FAILED: empty row returned success\n'; rc=1; }
    [ "$FAILED" = 1 ] || { printf '   SELFTEST FAILED: empty row did not fail the run\n'; rc=1; }
  done
  FAILED=0
  step_value "a real value" "42" || rc=1
  [ "$FAILED" = 0 ] || { printf '   SELFTEST FAILED: a real value was treated as empty\n'; rc=1; }
  printf '\n%s\n' "$(_c 1 'selftest: the same guard on the Open Library side')"
  python3 "${REPO_ROOT}/tests/e2e/borrow_flow.py" --selftest || rc=1
  if [ "$rc" = 0 ]; then
    printf '\n%s\n' "$(_c '1;32' 'SELFTEST PASS  empty rows print FAIL and fail the run, on both sides.')"
  else
    printf '\n%s\n' "$(_c '1;31' 'SELFTEST FAIL  the reporting guard does not hold.')"
  fi
  exit "$rc"
fi

# ── cleanup ──────────────────────────────────────────────────────────────────
# Only ever removes names carrying this run's own random id.

cleanup() {
  local code=$?
  if [ "$KEEP" = 1 ]; then
    printf '\n%s\n' "$(_c '33' "--keep given: leaving ${PREFIX}_* up. Remove with:")"
    printf '   docker rm -f %s %s %s 2>/dev/null; docker network rm %s\n' "$API" "$DB" "$OL" "$NET"
    return
  fi
  printf '\n%s\n' "$(_c '90' "cleaning up ${PREFIX}_*")"
  docker rm -f "$API" "$DB" "$OL" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  local leftovers
  leftovers="$(docker ps -aq --filter "name=^${PREFIX}_" 2>/dev/null | wc -l | tr -d ' ')"
  if [ "$leftovers" != "0" ]; then
    printf '%s\n' "$(_c '31' "WARNING: ${leftovers} ${PREFIX}_* container(s) survived cleanup")"
  else
    printf '%s\n' "$(_c '90' 'nothing of this run is left running')"
  fi
  exit "$code"
}
trap cleanup EXIT INT TERM

# ── preflight ────────────────────────────────────────────────────────────────

printf '%s\n' "$(_c 1 'Lenny <-> Open Library borrow seam, end to end')"
printf '%s\n' "$(_c '90' "run id ${RUN_ID}   repo ${REPO_ROOT}")"

command -v docker >/dev/null 2>&1 || cannot_run "docker is not on PATH."
docker info >/dev/null 2>&1 || cannot_run "the Docker daemon is not reachable (try: colima start)."

for image in "$LENNY_IMAGE" "$OL_IMAGE" "$PG_IMAGE"; do
  docker image inspect "$image" >/dev/null 2>&1 || cannot_run \
    "image '${image}' is not present locally. This harness does not build images
     (nginx's reader/admin upstreams cannot be built on a machine without
     buildx). Build or pull it first, or override with LENNY_IMAGE / OL_IMAGE /
     PG_IMAGE."
done

[ -f "${REPO_ROOT}/tests/oauth2/otp_stub.py" ] || cannot_run \
  "tests/oauth2/otp_stub.py is missing. It arrives with ArchiveLabs/lenny#230."

# The bind mount has to live under a path the Docker VM actually mounts. On
# Colima that is /Users/<you> only -- a /private/tmp path is silently CREATED
# empty inside the VM and the container reads nothing, with no error.
case "$REPO_ROOT" in
  /Users/*|/home/*) ;;
  *) cannot_run "the repo is at ${REPO_ROOT}, outside the paths a Docker VM
     typically mounts. A bind mount from there can silently resolve to an empty
     directory inside the VM." ;;
esac

if [ -z "$OL_CHECKOUT" ]; then
  for candidate in "${REPO_ROOT}/../openlibrary" /Users/*/Projects/openlibrary*; do
    if [ -f "${candidate}/openlibrary/core/provider_tokens.py" ]; then
      OL_CHECKOUT="$candidate"; break
    fi
  done
fi
[ -n "$OL_CHECKOUT" ] || cannot_run \
  "no Open Library checkout given. Pass --openlibrary /path/to/openlibrary (or set
   OL_CHECKOUT). It must be a branch that carries the Lenny borrow code."
OL_CHECKOUT="$(cd "$OL_CHECKOUT" 2>/dev/null && pwd)" || cannot_run "OL_CHECKOUT is not a directory."

for required in openlibrary/core/provider_tokens.py \
                openlibrary/plugins/upstream/lenny.py \
                openlibrary/plugins/upstream/borrow.py \
                openlibrary/core/schema.sql \
                openlibrary/tests/core/test_provider_tokens.py; do
  [ -f "${OL_CHECKOUT}/${required}" ] || cannot_run \
    "${OL_CHECKOUT} has no ${required}. That checkout does not carry the Lenny
     borrow code -- try a branch descended from openlibrary#13552 / #13687."
done

step "Preflight"
step_value "docker" "$(docker version --format '{{.Server.Version}}' 2>/dev/null)"
step_value "lenny image" "${LENNY_IMAGE} $(docker image inspect "$LENNY_IMAGE" --format '{{.Id}}' | cut -c8-19)"
step_value "openlibrary image" "${OL_IMAGE} $(docker image inspect "$OL_IMAGE" --format '{{.Id}}' | cut -c8-19)"
step_value "openlibrary checkout" "${OL_CHECKOUT} @ $(git -C "$OL_CHECKOUT" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
step_value "lenny worktree" "${REPO_ROOT} @ $(git -C "$REPO_ROOT" rev-parse --short HEAD)"

# `borrow()` is the only function on any branch that creates a loan, so it is
# required. `mediated_borrow()` decides whether the borrow button routes through
# Open Library at all -- a different operation, present only on
# openlibrary#13552-descended branches, and covered by its own step. These are
# reported separately because an earlier version of this harness treated them as
# two names for one function and would have reported a borrow that never
# happened.
OL_LENNY_PY="${OL_CHECKOUT}/openlibrary/plugins/upstream/lenny.py"
grep -qE '^def borrow\(' "$OL_LENNY_PY" || cannot_run \
  "${OL_LENNY_PY} defines no borrow(). That is the function that creates the
   loan, so there is nothing here to exercise. Wrong branch."
step_value "openlibrary lenny.borrow()" \
  "present (line $(grep -nE '^def borrow\(' "$OL_LENNY_PY" | cut -d: -f1)) -- creates the loan"
if grep -qE '^def mediated_borrow\(' "$OL_LENNY_PY"; then
  step_value "openlibrary lenny.mediated_borrow()" \
    "present (line $(grep -nE '^def mediated_borrow\(' "$OL_LENNY_PY" | cut -d: -f1)) -- routes the button; covered by its own step"
else
  step_value "openlibrary lenny.mediated_borrow()" \
    "absent on this checkout -- that step will report skip, not pass"
fi

# ── the deliberate-failure drill ─────────────────────────────────────────────
# A harness nobody has watched fail is not an instrument. Both modes name the
# step they are expected to turn red, so a run that goes red SOMEWHERE ELSE is
# itself a finding.

ADVERTISED_HOST="$API"
case "$BREAK_STEP" in
  "") ;;
  issuer)
    # Open Library is configured with a node issuer that does not resolve.
    # Expected: the node stack and the whole Lenny half are green; the Open
    # Library half goes red at its first call that leaves the process.
    ;;
  no-loan)
    # Step 17 skips the actual lenny.borrow() call and fabricates a plausible
    # response. Expected: green until the loan-count check in that same step,
    # which compares the node's loans before and after. This is the regression
    # guard for the defect that prompted it -- a borrow reported from a
    # response body rather than from a loan that exists.
    ;;
  advertised-issuer)
    # The node advertises a host nothing can resolve, while Open Library is
    # configured with the one that works. This is the documented hazard:
    # `provider_loans` builds its URL from the CONFIGURED issuer and would keep
    # working, while `node_refresher` follows DISCOVERY's token_endpoint and
    # would fail -- and a failed refresh DELETES the patron's grant. The
    # harness refuses to get that far: it compares advertised against
    # configured at the discovery step and stops there.
    ADVERTISED_HOST="unreachable-node.invalid"
    ;;
  *)
    echo "unknown --break mode: ${BREAK_STEP} (try: issuer, advertised-issuer, no-loan)" >&2
    exit "$EXIT_CANNOT_RUN" ;;
esac
[ -z "$BREAK_STEP" ] || printf '\n%s\n' "$(_c '1;33' "--break ${BREAK_STEP}: this run is EXPECTED to fail. Exit 0 would be the bug.")"

# ── bring the node up ────────────────────────────────────────────────────────

step "Start a throwaway node stack"
docker network create "$NET" >/dev/null 2>&1 || cannot_run "could not create network ${NET}."
step_value "network" "$NET"

docker run -d --name "$DB" --network "$NET" --network-alias db \
  -e POSTGRES_DB="$DB_NAME" -e POSTGRES_USER="$DB_USER" -e POSTGRES_PASSWORD="$DB_PASSWORD" \
  "$PG_IMAGE" >/dev/null 2>&1 || cannot_run "could not start postgres."
for _ in $(seq 1 60); do
  docker exec "$DB" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1 \
  || cannot_run "postgres never became ready."
step_value "postgres" "$DB ($(docker exec "$DB" psql -U "$DB_USER" -d "$DB_NAME" -tAc 'select version()' | cut -c1-24))"

# Open Library's own tables live in a second database in the same server: it is
# a throwaway either way, and one fewer container to leak.
docker exec "$DB" psql -U "$DB_USER" -d "$DB_NAME" -c "CREATE DATABASE ${OL_DB_NAME}" >/dev/null 2>&1 \
  || cannot_run "could not create the Open Library database."
step_value "openlibrary database" "${OL_DB_NAME} on ${DB}"

# nginx is bypassed on purpose: its lenny_reader / lenny_admin upstreams cannot
# be built on a machine without buildx, and nothing in this flow needs it.
#
# LENNY_HOST is the api container's own name so the discovery document
# advertises a host the Open Library container can resolve. This is load
# bearing: `provider_loans` builds its URL from the CONFIGURED issuer while
# `node_refresher` uses the issuer's DISCOVERED token_endpoint, so if the two
# hosts disagree, loans succeed and refresh fails -- and a failed refresh
# CLEARS the patron's grant.
docker run -d --name "$API" --network "$NET" --network-alias "$API" \
  -v "${REPO_ROOT}:/app" -w /app \
  -e PYTHONPATH=/app \
  -e TESTING=false \
  -e DB_TYPE=postgres -e DB_HOST=db -e DB_PORT=5432 \
  -e DB_USER="$DB_USER" -e DB_PASSWORD="$DB_PASSWORD" -e DB_NAME="$DB_NAME" \
  -e LENNY_HOST="$ADVERTISED_HOST" -e LENNY_PORT="$LENNY_PORT_IN" -e LENNY_PROXY= \
  -e LENNY_SEED="$LENNY_SEED" -e LENNY_WORKERS=1 -e LENNY_PRODUCTION=false \
  -e LENNY_LOG_LEVEL=warning \
  -e OTP_SERVER="http://127.0.0.1:${OTP_PORT}" \
  -e LENNY_LENDING_MODE=ol \
  -e OL_S3_ACCESS_KEY=e2e-not-a-real-key -e OL_S3_SECRET_KEY=e2e-not-a-real-secret \
  -e ADMIN_USERNAME=admin -e ADMIN_PASSWORD=e2e-admin \
  -e ADMIN_INTERNAL_SECRET=e2e-internal -e ADMIN_SALT=e2e-salt \
  -e LENNY_FORWARDED_ALLOW_IPS=127.0.0.1 \
  "$LENNY_IMAGE" \
  sh -c "exec python -m uvicorn lenny.app:app --host 0.0.0.0 --port ${LENNY_PORT_IN} --log-level warning" \
  >/dev/null 2>&1 || cannot_run "could not start the api container."

# Migrations before the first request: the app does not create its own schema.
sleep 2
if ! MIGRATE_OUT="$(docker exec "$API" alembic upgrade head 2>&1)"; then
  printf '%s\n' "$MIGRATE_OUT" | tail -20
  cannot_run "alembic could not migrate the throwaway database."
fi
step_value "schema" "alembic head = $(docker exec "$API" alembic current 2>/dev/null | tail -1 | awk '{print $1}')"

HEALTH=""
for _ in $(seq 1 60); do
  HEALTH="$(docker exec "$API" python -c "
import json,urllib.request
try:
    print(json.load(urllib.request.urlopen('http://127.0.0.1:${LENNY_PORT_IN}/.well-known/oauth-authorization-server', timeout=2))['issuer'])
except Exception:
    pass" 2>/dev/null)"
  [ -n "$HEALTH" ] && break
  sleep 1
done
if [ -z "$HEALTH" ]; then
  docker logs "$API" 2>&1 | tail -25
  cannot_run "the node never served its discovery document."
fi
step_value "node issuer (advertised)" "$HEALTH"

# ── the OTP stub ─────────────────────────────────────────────────────────────
# Started INSIDE the api container, so it is reachable at 127.0.0.1 from the
# only process that calls it and from nowhere else. No host port, no
# host.docker.internal, and no change to otp_stub.py's hardcoded bind.

step "Point OTP_SERVER at the stub (no human inbox)"
docker exec -d "$API" python3 tests/oauth2/otp_stub.py
OTP_PROBE=""
for _ in $(seq 1 30); do
  OTP_PROBE="$(docker exec "$API" python -c "
import json,urllib.request
try:
    r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:${OTP_PORT}/account/otp/issue?email=probe%40example.org', method='POST'), timeout=2)
    print(json.load(r).get('success',''))
except Exception:
    pass" 2>/dev/null)"
  [ -n "$OTP_PROBE" ] && break
  sleep 1
done
step_value "stub /account/otp/issue" "${OTP_PROBE} (OTP_SERVER=http://127.0.0.1:${OTP_PORT}, code ${OTP_CODE})" \
  || { step_fail "the OTP stub never answered; the patron leg cannot run"; exit "$EXIT_BROKEN"; }

# ── register Open Library as a client ────────────────────────────────────────

step "Register Open Library as an OAuth client"
# Loopback http is the only non-https redirect_uri the node accepts (RFC 8252).
# Nothing ever listens here: this flow drives the consent page directly, the
# way Open Library's own callback handler would be driven by a browser.
OL_REDIRECT_URI="http://127.0.0.1:8092/borrow/lenny/callback"
REG_OUT="$(docker exec "$API" python3 scripts/oauth2_client.py register "Open Library" "$OL_REDIRECT_URI" 2>&1)"
CLIENT_ID="$(printf '%s\n' "$REG_OUT" | awk '/client_id/ {print $2}' | head -1)"
CLIENT_SECRET="$(printf '%s\n' "$REG_OUT" | awk '/client_secret/ {print $2}' | head -1)"
if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
  printf '%s\n' "$REG_OUT" | tail -20
  step_fail "registration printed no client_id/client_secret"
  exit "$EXIT_BROKEN"
fi
step_value "client_id" "$CLIENT_ID"
step_value "client_secret" "${CLIENT_SECRET:0:6}… (${#CLIENT_SECRET} chars, printed once)"
step_value "redirect_uri" "$OL_REDIRECT_URI"

# ── a borrowable book ────────────────────────────────────────────────────────
# encrypted=True is the whole requirement: an open-access item answers
# `not_lendable`, which reads like a broken borrow and is not one.

step "Seed one lendable item"
SEED_OUT="$(docker exec "$API" python3 -c "
from lenny.core.db import session
from lenny.core.models import Item, FormatEnum
rows = []
for edition in (${EDITION_ID}, ${OL_EDITION_ID}):
    item = Item(openlibrary_edition=edition, encrypted=True, formats=FormatEnum.EPUB)
    session.add(item); session.commit()
    rows.append(f'{item.id}:{item.openlibrary_edition}:encrypted={item.encrypted}:{item.formats.name}')
print('|'.join(rows))
" 2>&1 | tail -1)"
case "$SEED_OUT" in
  *'|'*) : ;;
  *) printf '%s\n' "$SEED_OUT"; step_fail "could not seed an item"; exit "$EXIT_BROKEN" ;;
esac
step_value "item borrowed over the protocol" "$(echo "$SEED_OUT" | cut -d'|' -f1)"
step_value "item borrowed by Open Library's code" "$(echo "$SEED_OUT" | cut -d'|' -f2)"

# ── both halves of the flow ──────────────────────────────────────────────────
# Run inside an Open Library container on the same network, because the
# consumer side is where Open Library's code has to work, and because a single
# process means a single client IP -- Lenny binds the session cookie to it.

step "Walk the flow (patron leg, OAuth leg, borrow, and Open Library's half)"
printf '        %s\n' "$(_c '90' "in ${OL_IMAGE}, on ${NET}, against http://${API}:${LENNY_PORT_IN}")"

docker run --rm --name "$OL" --network "$NET" \
  -v "${OL_CHECKOUT}:/openlibrary:ro" \
  -v "${REPO_ROOT}/tests/e2e:/harness:ro" \
  -w /openlibrary \
  -e PYTHONPATH=/openlibrary \
  -e LENNY_ISSUER="http://${API}:${LENNY_PORT_IN}" \
  -e LENNY_CLIENT_ID="$CLIENT_ID" \
  -e LENNY_CLIENT_SECRET="$CLIENT_SECRET" \
  -e LENNY_REDIRECT_URI="$OL_REDIRECT_URI" \
  -e PATRON_EMAIL="$PATRON_EMAIL" \
  -e OTP_CODE="$OTP_CODE" \
  -e EDITION_ID="$EDITION_ID" \
  -e OL_EDITION_ID="$OL_EDITION_ID" \
  -e PROVIDER_NAME="$PROVIDER_NAME" \
  -e OL_DB_HOST=db -e OL_DB_PORT=5432 -e OL_DB_NAME="$OL_DB_NAME" \
  -e OL_DB_USER="$DB_USER" -e OL_DB_PASSWORD="$DB_PASSWORD" \
  -e BREAK_STEP="$BREAK_STEP" \
  -e STEP_OFFSET="$STEP_N" \
  "$OL_IMAGE" python /harness/borrow_flow.py
FLOW_RC=$?

printf '\n'
if [ "$FLOW_RC" = "$EXIT_CANNOT_RUN" ]; then
  printf '%s\n' "$(_c '1;33' 'SKIP  the Open Library half could not run (see its message above).')"
  exit "$EXIT_CANNOT_RUN"
fi
if [ "$FLOW_RC" != "0" ] || [ "$FAILED" = "1" ]; then
  if [ -n "$BREAK_STEP" ]; then
    printf '%s\n' "$(_c '1;33' "EXPECTED FAILURE  --break ${BREAK_STEP} produced a red step, above.")"
    printf '%s\n' "$(_c '90' '        Exit 1, which is what a real breakage would also give you. Check')"
    printf '%s\n' "$(_c '90' '        that the red step is the one this mode names, not a later one.')"
  else
    printf '%s\n' "$(_c '1;31' 'BROKEN  at least one step failed. The seam does not work.')"
  fi
  exit "$EXIT_BROKEN"
fi
if [ -n "$BREAK_STEP" ]; then
  printf '%s\n' "$(_c '1;31' "BROKEN  --break ${BREAK_STEP} was given and every step still passed.")"
  printf '%s\n' "$(_c '31' '        The harness cannot see the failure it was asked to produce.')"
  exit "$EXIT_BROKEN"
fi
printf '%s\n' "$(_c '1;32' 'PASS  both halves walked end to end, with values, against a live node.')"
exit "$EXIT_OK"
