#!/usr/bin/env python3
"""Both halves of the Lenny <-> Open Library borrow seam, in one process.

Run by ``tests/e2e/run_borrow_e2e.sh`` inside an Open Library container that is
on the throwaway node's Docker network. Not meant to be run by hand: it reads
its whole configuration from environment variables the driver sets.

Why one process, inside the Open Library image:

* **One client IP.** Lenny binds a patron's session cookie to the address it
  saw. Two processes on two addresses means the cookie minted by the first is
  rejected by the second, which surfaces as a redirect to the login screen and
  reads like a broken consent page.
* **One resolvable issuer.** The node advertises ``http://<api-container>:1337``
  because that is what Open Library has to be able to resolve. A host-side
  client cannot follow that discovery document at all.
* **The consumer half is Open Library's.** ``provider_tokens``, ``lenny.py`` and
  ``borrow.datetime_from_isoformat`` are the code that has to work; running the
  protocol beside them means the tokens exercised are the tokens stored.

Exit codes: 0 pass, 1 the seam is broken, 2 could not run.
"""

from __future__ import annotations

import os
import re
import sys
import base64
import hashlib
import secrets
import json
from typing import NoReturn
from urllib.parse import parse_qs, urlencode, urlparse

CANNOT_RUN = 2

try:
    import requests
except ImportError:  # pragma: no cover - preflight in the driver covers this
    print("SKIP  the Open Library image has no `requests`.", file=sys.stderr)
    raise SystemExit(CANNOT_RUN)


# ── output ───────────────────────────────────────────────────────────────────
# Same contract as the driver's: every row carries the value it rests on, and a
# row with no value prints FAIL rather than looking like a pass.

_N = int(os.environ.get("STEP_OFFSET", "0"))
_FAILED = False
_SKIPPED = 0


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def step(title: str) -> None:
    global _N
    _N += 1
    print(f"\n{_c('1;36', f'── step {_N}')} {_c('1', title)}")


def value(label: str, val) -> bool:
    """Print one result. An empty value is a failure, never a pass."""
    global _FAILED
    text = "" if val is None else str(val)
    if not text.strip():
        print(f"   {_c('31', 'FAIL')} {label} -> "
              f"{_c('31', '<empty> -- no value was produced here; this is not a pass')}")
        _FAILED = True
        return False
    print(f"   {_c('32', 'ok  ')} {label} -> {text}")
    return True


def note(text: str) -> None:
    print(f"        {_c('90', text)}")


def skipped(label: str, reason: str) -> None:
    """A check this checkout cannot carry -- neither a pass nor a failure.

    Three states, not two. `ok` means the property holds; `FAIL` means it does
    not; this means the thing being checked does not exist on this branch, and
    collapsing it into either of the others is how a run lies. It is counted
    and reported in the summary so an absent check cannot read as a present
    one.
    """
    global _SKIPPED
    _SKIPPED += 1
    print(f"   {_c('33', 'skip')} {label} -> {_c('33', reason)}")


def die(label: str, detail: str) -> NoReturn:
    print(f"   {_c('31', 'FAIL')} {label}")
    for line in str(detail).splitlines():
        print(f"        {_c('31', line)}")
    raise SystemExit(1)


def cannot_run(detail: str) -> NoReturn:
    print(f"\n{_c('1;33', 'SKIP')} {detail}")
    raise SystemExit(CANNOT_RUN)


# ── configuration ────────────────────────────────────────────────────────────
# Read inside main() rather than at import, so the module can be imported with
# no environment at all -- which is what makes the reporting guards above
# testable. `--selftest` does exactly that.

TIMEOUT = 20


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def handle_from(html: str) -> str:
    m = re.search(r'name="request" value="([^"]+)"', html)
    if not m:
        die("consent page carried no request handle",
            html[:400] if html else "(empty body)")
    return m.group(1)


def main() -> int:
    ISSUER = os.environ["LENNY_ISSUER"].rstrip("/")
    CLIENT_ID = os.environ["LENNY_CLIENT_ID"]
    CLIENT_SECRET = os.environ["LENNY_CLIENT_SECRET"]
    REDIRECT_URI = os.environ["LENNY_REDIRECT_URI"]
    PATRON_EMAIL = os.environ["PATRON_EMAIL"]
    OTP_CODE = os.environ["OTP_CODE"]
    EDITION_ID = int(os.environ["EDITION_ID"])
    PROVIDER_NAME = os.environ["PROVIDER_NAME"]
    BREAK_STEP = os.environ.get("BREAK_STEP", "").strip()

    session_header: dict[str, str] = {}
    s = requests.Session()

    # ── The patron signs in at the node ──────────────────────────────────────
    # Two POSTs, because that is what a patron does: ask for a code, then
    # present it. The OTP stub stands in for Open Library's inbox.
    step("Patron leg: sign in at the node (OTP)")
    r = s.post(f"{ISSUER}/v1/api/oauth/authorize",
               data={"email": PATRON_EMAIL}, timeout=TIMEOUT)
    if r.status_code != 200:
        die("POST email did not return the code screen",
            f"HTTP {r.status_code}: {r.text[:300]}")
    if "otp" not in r.text.lower():
        die("the code screen does not mention an OTP",
            "Lending is probably not configured: LENNY_LENDING_MODE must be `ol` "
            "AND both OL_S3_ACCESS_KEY and OL_S3_SECRET_KEY must be non-empty, or "
            "every OTP call answers `lending_not_configured`.\n" + r.text[:300])
    value("POST email", f"HTTP {r.status_code}, the node asked for a code")

    r = s.post(f"{ISSUER}/v1/api/oauth/authorize",
               data={"email": PATRON_EMAIL, "otp": OTP_CODE}, timeout=TIMEOUT)
    set_cookie = r.headers.get("set-cookie", "")
    m = re.search(r"session=([^;]+)", set_cookie)
    if not m:
        die("no session cookie came back from the OTP redemption",
            f"HTTP {r.status_code}; Set-Cookie: {set_cookie!r}\n{r.text[:300]}")
    cookie = m.group(1)
    # The cookie is minted with secure=True, hardcoded. Over plain http a client
    # that stores cookies normally will never send it back, so it goes on every
    # later request as an explicit header instead. This is not a workaround for
    # a bug; it is what a `secure` cookie means.
    session_header = {"Cookie": f"session={cookie}"}
    value("OTP redeemed", f"HTTP {r.status_code}, session cookie {cookie[:16]}… "
                          f"({len(cookie)} chars)")
    note("sent as an explicit Cookie: header from here on -- the node sets it "
         "secure=True, so a plain-http client would never send it back")

    # ── Discovery ────────────────────────────────────────────────────────────
    step("Discover the node (RFC 8414)")
    r = s.get(f"{ISSUER}/.well-known/oauth-authorization-server", timeout=TIMEOUT)
    if r.status_code != 200:
        die("no discovery document", f"HTTP {r.status_code}: {r.text[:300]}")
    meta = r.json()
    value("issuer", meta.get("issuer"))
    value("authorization_endpoint", meta.get("authorization_endpoint"))
    value("token_endpoint", meta.get("token_endpoint"))
    value("scopes_supported", " ".join(meta.get("scopes_supported") or []))
    if "S256" not in (meta.get("code_challenge_methods_supported") or []):
        die("the node does not advertise PKCE S256",
            json.dumps(meta.get("code_challenge_methods_supported")))
    value("code_challenge_methods_supported", " ".join(meta["code_challenge_methods_supported"]))
    if meta["issuer"].rstrip("/") != ISSUER:
        die("the node advertises an issuer that is not the one we configured",
            f"advertised {meta['issuer']!r}, configured {ISSUER!r}. Set LENNY_HOST "
            "to a name this container can resolve -- otherwise loans work and "
            "refresh fails, and a failed refresh CLEARS the patron's grant.")

    # ── Authorization request + consent ──────────────────────────────────────
    step("Authorization request with PKCE, then consent")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "loans:read borrow",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    r = s.get(f"{meta['authorization_endpoint']}?{urlencode(params)}",
              headers=session_header, timeout=TIMEOUT, allow_redirects=False)
    if r.status_code == 303 and "/oauth/authorize" in r.headers.get("location", ""):
        die("the node did not recognise the session cookie",
            "it bounced us to its own login. The cookie is bound to the client IP "
            "that minted it, so every call in this flow has to come from one host.")
    if r.status_code != 200:
        die("the consent page did not render",
            f"HTTP {r.status_code}: {r.text[:300]}")
    handle = handle_from(r.text)
    value("consent page", f"HTTP 200, hidden request handle {handle[:18]}… "
                          f"({len(handle)} chars)")
    note("the form carries one opaque handle, not the request parameters, so a "
         "consumer cannot alter what the patron approved")

    r = s.post(f"{ISSUER}/v1/api/oauth2/authorize",
               headers=session_header,
               data={"request": handle, "decision": "allow"},
               timeout=TIMEOUT, allow_redirects=False)
    if r.status_code != 303:
        die("consent did not redirect back to the client",
            f"HTTP {r.status_code}: {r.text[:300]}")
    returned = parse_qs(urlparse(r.headers["location"]).query)
    code = (returned.get("code") or [""])[0]
    iss = (returned.get("iss") or [""])[0]
    if returned.get("state", [None])[0] != state:
        die("state mismatch on the redirect", f"sent {state!r}, got {returned.get('state')!r}")
    value("redirect", f"303 -> {urlparse(r.headers['location']).netloc}{urlparse(r.headers['location']).path}")
    value("code", f"{code[:14]}… ({len(code)} chars, single use)")
    value("state", f"{state[:12]}… verified")
    value("iss", iss)

    # ── Token exchange ───────────────────────────────────────────────────────
    step("Exchange the code for tokens (back channel)")
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = s.post(meta["token_endpoint"],
               headers={"Authorization": f"Basic {basic}"},
               data={"grant_type": "authorization_code", "code": code,
                     "redirect_uri": REDIRECT_URI, "code_verifier": verifier},
               timeout=TIMEOUT)
    if r.status_code != 200:
        die("token exchange failed", f"HTTP {r.status_code}: {r.text[:300]}")
    tok = r.json()
    value("access_token", f"{tok['access_token'][:14]}… ({len(tok['access_token'])} chars)")
    value("refresh_token", f"{tok['refresh_token'][:14]}… ({len(tok['refresh_token'])} chars)")
    value("expires_in", f"{tok['expires_in']}s")
    value("scope", tok.get("scope"))
    value("token_type", tok.get("token_type"))

    # ── Borrow ───────────────────────────────────────────────────────────────
    step(f"Borrow edition {EDITION_ID} (scope borrow)")
    r = s.post(f"{ISSUER}/v1/api/oauth2/borrow",
               headers={"Authorization": f"Bearer {tok['access_token']}"},
               data={"edition_id": EDITION_ID}, timeout=TIMEOUT)
    if r.status_code != 201:
        detail = r.text[:300]
        if '"not_lendable"' in detail:
            detail += ("\nThe item exists but is open access. A borrowable item "
                       "must be seeded with encrypted=True.")
        die(f"borrow returned HTTP {r.status_code}", detail)
    loan = r.json()
    value("HTTP", f"201 {loan.get('status')}")
    value("edition_id", loan.get("edition_id"))
    value("due_at", loan.get("due_at") or "no expiry")

    # ── Loans ────────────────────────────────────────────────────────────────
    step("Read the patron's loans (scope loans:read)")
    r = s.get(f"{ISSUER}/v1/api/oauth2/loans",
              headers={"Authorization": f"Bearer {tok['access_token']}"},
              timeout=TIMEOUT)
    if r.status_code != 200:
        die("loans call failed", f"HTTP {r.status_code}: {r.text[:300]}")
    loans = r.json().get("loans") or []
    value("loan count", len(loans))
    for entry in loans:
        value(f"loan edition {entry.get('edition_id')}",
              f"due {entry.get('due_at') or 'no expiry'}")
    if not any(int(e.get("edition_id", -1)) == EDITION_ID for e in loans):
        die("the loan just created is not in the patron's loans",
            json.dumps(loans)[:400])

    from ol_half import run_openlibrary_half  # noqa: E402  (imported late on purpose)
    rc = run_openlibrary_half(
        step=step, value=value, note=note, die=die, cannot_run=cannot_run,
        skipped=skipped,
        issuer=ISSUER, provider_name=PROVIDER_NAME, client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI,
        patron_email=PATRON_EMAIL, edition_id=EDITION_ID,
        grant=tok, break_step=BREAK_STEP,
    )
    if rc:
        return rc
    if _SKIPPED:
        print(f"\n{_c('33', f'{_SKIPPED} check(s) were SKIPPED -- see the skip rows above.')}")
        print(f"{_c('90', 'A skipped check is not a passing one. What it covers is unverified.')}")
    return 1 if _FAILED else 0


def selftest() -> int:
    """Prove the reporting guard fires, rather than asserting that it does.

    A row printed with no value is the one failure mode this harness cannot
    afford, because it reads exactly like a pass. So the check is: call
    `value()` with an empty value and confirm it both prints FAIL and flips the
    run to failed.
    """
    global _FAILED
    print("selftest: a row with no value must not read as a pass")
    for empty in ("", None, "   ", "\n"):
        _FAILED = False
        ok = value(f"deliberately empty ({empty!r})", empty)
        if ok or not _FAILED:
            print(f"   SELFTEST FAILED: value({empty!r}) did not register as a failure")
            return 1
    _FAILED = False
    if not value("a real value", "42") or _FAILED:
        print("   SELFTEST FAILED: a non-empty value was not accepted")
        return 1
    before = _SKIPPED
    skipped("a check this branch cannot carry", "absent here; not a pass")
    if _SKIPPED != before + 1 or _FAILED:
        print("   SELFTEST FAILED: skipped() must count, and must not fail the run")
        return 1
    print("selftest: ok -- empty rows print FAIL and fail the run; real values pass")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    try:
        raise SystemExit(main())
    except requests.RequestException as exc:
        print(f"\n{_c('1;31', 'FAIL')} the node was unreachable: {exc}")
        raise SystemExit(1)
