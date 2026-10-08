"""/shelf accepts the OAuth2 bearer token a reading app gets from the PKCE flow.

A reading app (e.g. the Book Server) holds an access token, not Lenny's login
cookie. /shelf only understood the cookie, so the app's bookshelf page said
"Not signed in" even with a perfectly valid token.
"""

import os
from unittest.mock import patch

import pytest
import requests
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core.api import LennyAPI  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import FormatEnum, Item, Loan  # noqa: E402
from lenny.core.oauth2 import AccessToken, OAuthClient  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402

ME = "shelf-patron@example.com"
SOMEONE_ELSE = "someone-else@example.com"
EDITION = 910000777


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_access_tokens", "oauth_clients", "loans", "items"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


def token_for(email, scope="loans:read"):
    client, _ = OAuthClient.register(
        name="Reader", redirect_uris=["opds://test"], scopes=[scope], is_confidential=False)
    access, _refresh, _row = AccessToken.issue(
        client_id=client.client_id, patron_email_hash=hash_email(email), scope=scope)
    return access


def lend_to(email):
    item = Item(openlibrary_edition=EDITION, encrypted=True, formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    db.add(Loan(item_id=item.id, patron_email_hash=hash_email(email)))
    db.commit()


def shelf(token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app).get("/v1/api/shelf", headers=headers)


def test_without_credentials_the_shelf_is_a_401_with_the_auth_document():
    r = shelf()
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/opds-authentication+json")


def test_a_valid_token_is_signed_in_and_sees_its_own_loans():
    lend_to(ME)
    with patch.object(LennyAPI, "get_shelf_feed", wraps=LennyAPI.get_shelf_feed) as spy, \
         patch("lenny.core.api.LennyDataProvider.search", side_effect=requests.exceptions.ConnectionError("no network")):
        r = shelf(token_for(ME))
    # The feed builder was handed the patron's hash, flagged as already hashed.
    args, kwargs = spy.call_args
    assert args[0] == hash_email(ME) and kwargs["hashed"] is True
    # Open Library being unreachable degrades to an empty shelf, not a sign-in prompt.
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/opds+json")


def test_the_loan_lookup_accepts_a_hashed_identity():
    lend_to(ME)
    assert [l.openlibrary_edition for l in LennyAPI.get_borrowed_items(hash_email(ME), hashed=True)] == [EDITION]
    assert [l.openlibrary_edition for l in LennyAPI.get_borrowed_items(ME)] == [EDITION]


def test_attack_a_token_only_ever_sees_its_own_patrons_loans():
    lend_to(ME)
    assert LennyAPI.get_borrowed_items(hash_email(SOMEONE_ELSE), hashed=True) == []


def test_attack_a_token_without_loans_read_is_refused():
    assert shelf(token_for(ME, scope="borrow")).status_code == 401


def test_attack_a_made_up_token_is_refused():
    assert shelf("not-a-real-token").status_code == 401


def test_attack_a_revoked_client_cuts_the_token_off():
    token = token_for(ME)
    client_id = AccessToken.authenticate(token).client_id
    OAuthClient.disable(client_id)
    assert shelf(token).status_code == 401
