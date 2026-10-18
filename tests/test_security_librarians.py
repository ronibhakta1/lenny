"""Security regressions for the librarian account routes."""

import os

import pytest
from fastapi.testclient import TestClient

from tests.test_librarians import PASSWORD, clean_table, make  # noqa: F401

os.environ.setdefault("TESTING", "true")


@pytest.fixture
def api(monkeypatch):
    from lenny.app import app
    from lenny.core import auth
    monkeypatch.setattr(auth, "ADMIN_INTERNAL_SECRET", "s3cret")
    client = TestClient(app, raise_server_exceptions=False)

    def call(method, path, row=None, **kw):
        headers = {"X-Admin-Internal-Secret": "s3cret",
                   "Authorization": "Bearer " + auth.issue_admin_token(row.id if row else None)}
        return client.request(method, "/v1/api" + path, headers=headers, **kw)
    return call


def test_a_librarian_cannot_disable_their_own_account(api):
    from lenny.core import librarians
    root = make()
    make("backup", "backup@example.org")  # a second super, so the last-super guard is not what stops it
    r = api("POST", f"/admin/librarians/{root.id}/disable", root, json={"disabled": True})
    assert r.status_code == 400 and r.json()["detail"] == "You cannot disable your own account."
    assert librarians.get_active(root.id)
    # re-enabling yourself is not a disable
    assert api("POST", f"/admin/librarians/{root.id}/disable", root, json={"disabled": False}).status_code == 200
