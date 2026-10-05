"""Open Library's half of the borrow seam, exercised against a live Lenny node.

Imported by ``borrow_flow.py``, which has already walked the node's side and
holds a real grant. Nothing here is a stand-in: it is Open Library's own
``provider_tokens``, ``plugins/upstream/lenny.py`` and
``plugins/upstream/borrow.datetime_from_isoformat``, imported from the checkout
bind-mounted at ``/openlibrary``, talking to the node over the network.

Two things this deliberately does NOT do, because pretending otherwise is how a
green run stops meaning anything:

* It does not run Open Library's web app. There is no request context, no
  templates and no infogami app -- so nothing here proves the *pages* work. It
  proves the functions behind them do.
* It creates ``provider_tokens`` itself, from the DDL constant that lives in
  Open Library's own test file, because **that table is not in
  ``openlibrary/core/schema.sql``** on any branch shipped so far. A green run
  here is therefore not evidence that a deployed Open Library has the table.
  See openlibrary#13689.
"""

from __future__ import annotations

import ast
import datetime
import os
import re

OL_ROOT = "/openlibrary"


def _ddl_from_test_file() -> tuple[str, str]:
    """``POSTGRES_DDL`` as the checkout actually spells it, plus its provenance.

    Read rather than copied: a constant duplicated here would go stale silently
    the first time Open Library changed the table, and the harness would keep
    passing against a schema nobody ships.
    """
    path = f"{OL_ROOT}/openlibrary/tests/core/test_provider_tokens.py"
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "POSTGRES_DDL":
                return node.value.value, f"{path}:{node.lineno}"
    raise LookupError(f"no POSTGRES_DDL constant in {path}")


def _acquisitions_ddl() -> tuple[str, str]:
    """The ``acquisitions`` CREATE TABLE, read out of Open Library's schema.sql."""
    path = f"{OL_ROOT}/openlibrary/core/schema.sql"
    text = open(path).read()
    m = re.search(r"^CREATE TABLE acquisitions \(.*?^\);", text, re.S | re.M)
    if not m:
        raise LookupError(f"no `CREATE TABLE acquisitions` in {path}")
    line = text[: m.start()].count("\n") + 1
    return m.group(0), f"{path}:{line}"


def run_openlibrary_half(*, step, value, note, die, cannot_run, skipped, issuer,
                         provider_name, client_id, client_secret, redirect_uri,
                         patron_email, edition_id, grant, break_step):
    username = "e2e_patron"
    ol_edition_id = int(os.environ["OL_EDITION_ID"])
    ol_edition_key = f"/books/OL{ol_edition_id}M"

    # ── Bootstrap ────────────────────────────────────────────────────────────
    step("Open Library: bootstrap (db, crypto secret, node config)")
    import web
    from infogami import config

    # web.py echoes every statement when this is on, and the provider_tokens
    # INSERT carries the encrypted token. Off before anything touches the DB.
    web.config.debug = False

    # encrypt_token/decrypt_token derive a Fernet key from this. Any string
    # works; it only has to be the same one across a write and a later read.
    config.infobase = {"secret_key": "e2e-not-a-real-secret"}

    db_parameters = {
        "dbn": "postgres",
        "host": os.environ["OL_DB_HOST"],
        "port": int(os.environ["OL_DB_PORT"]),
        "db": os.environ["OL_DB_NAME"],
        "user": os.environ["OL_DB_USER"],
        "pw": os.environ["OL_DB_PASSWORD"],
    }
    web.config.db_parameters = db_parameters

    node_issuer = issuer
    if break_step == "issuer":
        node_issuer = "http://not-a-real-node.invalid:1337"
        note(f"--break issuer: Open Library is configured with {node_issuer}, "
             "which does not resolve. Everything downstream of it must go red.")

    node_display_name = "E2E Test Library"
    config.lenny_nodes = {
        provider_name: {
            "issuer": node_issuer,
            "client_id": client_id,
            "client_secret": client_secret,
            # Read back in the mediated_borrow step. A node IS a library, so the
            # name a patron is shown has to come from the node's own config; a
            # constant that happened to match would prove nothing.
            "name": node_display_name,
        }
    }
    config.lenny_redirect_uri = redirect_uri

    from openlibrary.core import db as dbm
    dbm._get_db.cache_clear()  # _get_db is functools.cache'd; params set above

    value("db_parameters", f"{db_parameters['dbn']}://{db_parameters['user']}@"
                           f"{db_parameters['host']}:{db_parameters['port']}/{db_parameters['db']}")
    value("infogami.config.infobase.secret_key", f"set ({len(config.infobase['secret_key'])} chars)")
    value("config.lenny_nodes", f"{{{provider_name!r}: issuer={node_issuer}}}")
    value("config.lenny_redirect_uri", config.lenny_redirect_uri)

    # ── Tables ───────────────────────────────────────────────────────────────
    step("Open Library: create the tables its Lenny code reads")
    tokens_ddl, tokens_src = _ddl_from_test_file()
    acq_ddl, acq_src = _acquisitions_ddl()
    oldb = dbm.get_db()
    oldb.query(tokens_ddl)
    oldb.query(acq_ddl)
    value("provider_tokens", f"created from {tokens_src}")
    note("provider_tokens is NOT in openlibrary/core/schema.sql -- it exists only "
         "in that test constant. A green run here is not evidence a deployed "
         "Open Library has the table (openlibrary#13689).")
    value("acquisitions", f"created from {acq_src}")
    value("tables present", ", ".join(sorted(
        r.tablename for r in oldb.query(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"))))

    # ── The borrow link exists as a row ──────────────────────────────────────
    step("Open Library: the node is found from the acquisitions table")
    from openlibrary.core.acquisitions import Acquisition
    from openlibrary.plugins.upstream import lenny

    oldb.query(
        "INSERT INTO acquisitions (work_id, edition_id, provider_name, local_id, data) "
        "VALUES ($work, $edition, $provider, $local, $data)",
        vars={"work": 1, "edition": ol_edition_id, "provider": provider_name,
              "local": f"lenny-{ol_edition_id}", "data": "{}"},
    )
    rows = Acquisition.get_by_edition(ol_edition_id)
    value("acquisitions rows for that edition",
          ", ".join(f"{r.provider_name} local_id={r.local_id}" for r in rows))
    found = lenny.node_for_edition(ol_edition_key)
    if not found:
        die(f"node_for_edition({ol_edition_key}) found no configured node",
            f"configured: {sorted(lenny.nodes())}; rows: {[r.provider_name for r in rows]}")
    value(f"node_for_edition({ol_edition_key})", f"{found[0]} -> {found[1]['issuer']}")

    # ── Store the grant ──────────────────────────────────────────────────────
    step("Open Library: store the patron's grant (encrypted, one row per node)")
    from openlibrary.core.provider_tokens import Grant, ProviderToken, TokenRefreshFailed

    expires = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) + datetime.timedelta(
        seconds=int(grant["expires_in"]))
    ProviderToken.upsert(username, provider_name, Grant(
        access_token=grant["access_token"],
        refresh_token=grant["refresh_token"],
        expires=expires,
        scope=grant.get("scope", ""),
    ))
    stored = ProviderToken.get(username, provider_name)
    if stored is None:
        die("ProviderToken.get returned nothing right after upsert", "the row did not land")
    ciphertext = oldb.query(
        "SELECT access_token FROM provider_tokens WHERE username=$u AND provider_name=$p",
        vars={"u": username, "p": provider_name})[0].access_token
    value("row", f"username={username} provider={provider_name}")
    value("stored at rest", f"{ciphertext[:18]}… ({len(ciphertext)} chars) -- "
                            f"{'ENCRYPTED' if ciphertext != grant['access_token'] else 'PLAINTEXT'}")
    if ciphertext == grant["access_token"]:
        die("the access token is stored in plaintext", "encrypt_token did nothing")
    value("decrypted round-trip", "matches the token the node issued"
          if stored.access_token == grant["access_token"] else "")
    if stored.access_token != grant["access_token"]:
        die("what came back out is not what went in", "decrypt_token did not round-trip")
    value("scope", stored.scope)
    value("expires", stored.expires.isoformat())
    value("providers for this patron", ", ".join(ProviderToken.get_providers(username)))

    # ── Which borrow does Open Library offer for this book? ──────────────────
    # `mediated_borrow` decides whether the button sends the patron through
    # Open Library's own handshake or straight to the node's sign-in. It
    # creates no loan and never contacts the node -- it is a routing decision,
    # which is exactly why it is its OWN step and not a stand-in for the call
    # that borrows. An earlier version of this file picked between the two with
    # `getattr(lenny, "mediated_borrow", None) or lenny.borrow`, as though they
    # were two names for one function. They are two different operations, and
    # had their arities matched, this harness would have reported a borrow
    # while creating no loan.
    step("Open Library: mediated_borrow() routes the borrow button")
    mediated = getattr(lenny, "mediated_borrow", None)
    if mediated is None:
        skipped("lenny.mediated_borrow()",
                "absent from this checkout -- routing is decided elsewhere on "
                "this branch, so nothing here covers it")
        note("present on openlibrary#13552-descended branches; absent on #13687. "
             "This is a statement about the checkout, not about the seam.")
    else:
        offered = mediated(ol_edition_key)
        if not offered:
            die(f"mediated_borrow({ol_edition_key}) offered nothing",
                "a configured node lends this edition and an acquisitions row "
                "says so, so the borrow button should route through Open "
                "Library. None means the patron is sent to the node's own "
                "sign-in instead, and nothing on this side is exercised.")
        path, library = offered
        value(f"mediated_borrow({ol_edition_key})", f"{path!r}, {library!r}")
        if f"OL{ol_edition_id}M" not in path:
            die("the routing path does not name the edition asked for",
                f"{path!r} should contain OL{ol_edition_id}M")
        value("path names the edition", f"OL{ol_edition_id}M in {path}")
        if library != node_display_name:
            die("the library name shown to the patron did not come from config",
                f"configured name={node_display_name!r}, got {library!r}. The "
                "node's own `name` is what a patron should be shown -- see "
                "lenny.node_display_name().")
        value("library name came from the node's config", repr(library))

        # The control. A function that returns a plausible tuple for every
        # edition would pass everything above; what makes the answer mean
        # something is that it declines an edition no configured node lends.
        absent_key = "/books/OL999999999M"
        declined = mediated(absent_key)
        if declined is not None:
            die("mediated_borrow offered a node for an edition nobody lends",
                f"{absent_key} has no acquisitions row, so this must be None; "
                f"got {declined!r}. Without this, the check above would pass "
                "for a function that answers yes to everything.")
        value(f"control: mediated_borrow({absent_key})",
              "None -- declines an edition no configured node lends")

    # ── Borrow through Open Library's own code ───────────────────────────────
    # `lenny.borrow` unconditionally: it is the only function on any branch
    # that creates a loan.
    step(f"Open Library: borrow edition {ol_edition_id} via lenny.borrow()")

    def _node_loan_keys():
        """Books this patron holds at the node, read through OL's own code."""
        return sorted(loan.get("book") for loan in lenny.provider_loans(username).loans)

    before_keys = _node_loan_keys()
    value("loans at the node before", f"{len(before_keys)} -- {', '.join(before_keys) or 'none'}")

    token = lenny.access_token_for(username, provider_name)
    if not token:
        die("access_token_for returned nothing",
            "the stored grant is unusable; the patron would be sent back through "
            "authorization. If --break issuer is on, this is the expected red.")
    value("access_token_for", f"{token[:14]}… ({len(token)} chars)")

    if break_step == "no-loan":
        note("--break no-loan: skipping the actual lenny.borrow() call and "
             "fabricating its response. The loan-count check below must catch it.")
        result = {"status": "borrowed", "edition_id": ol_edition_id,
                  "due_at": "2099-01-01T00:00:00+00:00"}
    else:
        try:
            result = lenny.borrow({"issuer": node_issuer}, token, ol_edition_id)
        except lenny.LennyBorrowError as exc:
            die(f"lenny.borrow() refused: {exc.error}",
                f"HTTP {exc.status} -- {exc.message}")
        except Exception as exc:  # noqa: BLE001 - the node is the thing under test
            die("lenny.borrow() could not reach the node",
                f"{type(exc).__name__}: {exc}")
    value("node response status", result.get("status"))
    value("node response edition_id", result.get("edition_id"))
    result_due_at = result.get("due_at")
    value("node response due_at", result_due_at or "no expiry")

    # A borrow that returns a plausible dict without creating a loan is the
    # exact failure this step exists to make impossible. The node's own loan
    # list is the outcome; the response body is only the operation.
    after_keys = _node_loan_keys()
    value("loans at the node after", f"{len(after_keys)} -- {', '.join(after_keys) or 'none'}")
    gained = sorted(set(after_keys) - set(before_keys))
    if gained != [ol_edition_key]:
        die("the borrow returned, but the node gained no such loan",
            f"before={before_keys} after={after_keys} gained={gained}, "
            f"expected exactly ['{ol_edition_key}'].\n"
            "A response body says the call completed. Only the node's loan list "
            "says a loan exists, and those are different claims.")
    value("the node gained exactly this loan", gained[0])

    # ── Refresh, with the patron's row locked ────────────────────────────────
    # Forced by backdating the stored expiry, because a token minted a minute
    # ago will not refresh on its own and an unexercised refresh path is the
    # one that breaks in production. This is also the step that catches an
    # issuer the node advertises but Open Library cannot resolve: node_refresher
    # follows DISCOVERY's token_endpoint, while provider_loans builds from the
    # CONFIGURED issuer -- so a mismatch shows up here and nowhere else, and a
    # failed refresh DELETES the grant rather than retrying.
    step("Open Library: force a refresh through node_refresher()")
    backdated = (datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
                 - datetime.timedelta(hours=1))
    oldb.query(
        "UPDATE provider_tokens SET expires=$e WHERE username=$u AND provider_name=$p",
        vars={"e": backdated, "u": username, "p": provider_name},
    )
    value("stored expiry backdated to", "1 hour ago (so get_fresh must refresh)")
    node = lenny.nodes()[provider_name]
    refresher = lenny.node_refresher(node)  # the node DICT, never the provider name
    note("node_refresher takes the node dict; passing the provider name raises "
         "`TypeError: string indices must be integers` from inside lenny.py and "
         "reads like a library bug")
    try:
        fresh = ProviderToken.get_fresh(username, provider_name, refresher)
    except TokenRefreshFailed as exc:
        die("the refresh failed and the patron's grant has been DELETED",
            f"{exc}\nThis is what an issuer Open Library cannot resolve looks "
            f"like. Configured issuer: {node['issuer']}")
    value("refreshed access_token", f"{fresh.access_token[:14]}… "
          f"({'new' if fresh.access_token != grant['access_token'] else 'UNCHANGED'})")
    if fresh.access_token == grant["access_token"]:
        die("get_fresh returned the same access token", "no refresh actually happened")
    value("refreshed expires", fresh.expires.isoformat() if fresh.expires else "")
    value("refreshed scope", fresh.scope)
    value("grant still present after refresh",
          "yes" if ProviderToken.get(username, provider_name) else "")

    # ── The merged loans lookup ──────────────────────────────────────────────
    step("Open Library: provider_loans() merges the node's loans")
    result = lenny.provider_loans(username)
    value("loans", len(result.loans))
    value("unreachable", ", ".join(result.unreachable) or "none")
    value("unauthorized", ", ".join(result.unauthorized) or "none")
    if result.unreachable or result.unauthorized:
        die("the node was unreachable or rejected the token",
            f"unreachable={result.unreachable} unauthorized={result.unauthorized}")
    if not result.loans:
        die("provider_loans returned no loans",
            "the node has at least the loan this run just created. Note that "
            "provider_loans never raises and returns empty when lenny_nodes is "
            "unset -- an empty result is not evidence the node is healthy.")
    for loan in result.loans:
        value(f"loan {loan.get('book')}",
              f"expiry={loan.get('expiry')!r} provider={loan.get('provider')!r} "
              f"resource_type={loan.get('resource_type')!r} read_url={loan.get('read_url')}")
    books = {loan.get("book") for loan in result.loans}
    expected = {f"/books/OL{edition_id}M", f"/books/OL{ol_edition_id}M"}
    value("both books this run borrowed are present",
          "yes" if expected <= books else "")
    if not expected <= books:
        die("the merged lookup is missing a book this run just borrowed",
            f"expected {sorted(expected)}, got {sorted(books)}")

    # ── The last hop: can the loans page render it? ──────────────────────────
    step("Open Library: datetime_from_isoformat() on what the node returned")
    from openlibrary.plugins.upstream.borrow import datetime_from_isoformat

    checked = 0
    for loan in result.loans:
        expiry = loan.get("expiry")
        try:
            parsed = datetime_from_isoformat(expiry)
        except (ValueError, TypeError) as exc:
            die("the loans page cannot parse the expiry this node returned",
                f"{expiry!r} -> {type(exc).__name__}: {exc}\n"
                "This is the whole seam: provider_loans does not raise on a "
                "timezone-aware due_at, it passes the string through to a "
                "renderer that cannot parse it, and the patron loses every loan "
                "they have -- including their Internet Archive ones.")
        value(f"datetime_from_isoformat({expiry!r})", repr(parsed))
        checked += 1
    if not checked:
        die("no expiry was checked", "there was nothing to parse, so this step proved nothing")

    # The control, without which the step above proves nothing. The node's own
    # `due_at` is timezone-aware, and `datetime_from_isoformat` -- which is what
    # the loans page calls -- cannot parse a timezone-aware string at all. If
    # this raises nothing, `_expiry` has stopped normalising and the step above
    # was green for the wrong reason.
    raw_due_at = result_due_at
    try:
        datetime_from_isoformat(raw_due_at)
    except (ValueError, TypeError) as exc:
        value(f"control: the node's RAW due_at {raw_due_at!r}",
              f"rejected, {type(exc).__name__}: {exc}")
    else:
        die("the control did not fire",
            f"datetime_from_isoformat parsed the node's raw {raw_due_at!r}. Either "
            "the node stopped sending a timezone or the parser changed -- either "
            "way the step above is no longer testing what it claims, because "
            "lenny._expiry would have nothing left to normalise.")

    return 0
