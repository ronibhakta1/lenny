"""Librarians (#234): admin accounts in the database, hashed, with a protected super user."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("TESTING", "true")

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_table():
    from lenny.core.db import Base, engine, session
    from lenny.core import librarians  # noqa: F401
    Base.metadata.create_all(engine)
    session.remove()
    yield
    session.remove()
    with engine.begin() as c:
        c.exec_driver_sql("delete from librarians")


def make(username="root", email="root@example.org", is_super=True):
    from lenny.core import librarians
    return librarians.create(username, email, PASSWORD, is_super=is_super)


def test_password_is_stored_as_a_hash_and_verifies():
    from lenny.core import librarians
    row = make()
    assert row.password_hash.startswith("$argon2") and PASSWORD not in row.password_hash
    assert librarians.authenticate("root", PASSWORD).id == row.id
    assert librarians.authenticate("root", "wrong password!!") is None
    assert librarians.authenticate("nobody", PASSWORD) is None


def test_disabled_librarian_cannot_log_in():
    from lenny.core import librarians
    make()
    other = make("second", "second@example.org", is_super=False)
    librarians.set_disabled(other.id, True)
    assert librarians.authenticate("second", PASSWORD) is None
    librarians.set_disabled(other.id, False)
    assert librarians.authenticate("second", PASSWORD)


def test_last_enabled_super_user_cannot_be_disabled():
    from lenny.core import librarians
    root = make()
    with pytest.raises(librarians.LibrarianError):
        librarians.set_disabled(root.id, True)
    second = make("backup", "backup@example.org")
    librarians.set_disabled(root.id, True)
    with pytest.raises(librarians.LibrarianError):
        librarians.set_disabled(second.id, True)


@pytest.mark.parametrize("username,email,password", [
    ("", "a@b.org", PASSWORD),
    ("x", "not-an-email", PASSWORD),
    ("x", "a@b.org", "short"),
])
def test_invalid_input_is_refused(username, email, password):
    from lenny.core import librarians
    with pytest.raises(librarians.LibrarianError):
        librarians.create(username, email, password)


def test_duplicate_username_or_email_is_refused():
    from lenny.core import librarians
    make()
    with pytest.raises(librarians.LibrarianError):
        librarians.create("root", "other@example.org", PASSWORD)
    with pytest.raises(librarians.LibrarianError):
        librarians.create("other", "ROOT@example.org", PASSWORD)


def test_super_user_without_email_gets_one_through_update():
    from lenny.core import librarians
    from lenny.core.db import session
    row = librarians.Librarian(username="admin", password_hash="x", is_super=True)
    session.add(row); session.commit()
    assert row.email is None
    assert librarians.update(row.id, email="Admin@Example.org").email == "admin@example.org"


def test_authenticate_admin_issues_a_token_only_for_a_librarian():
    from lenny.core import auth
    make()
    assert auth.verify_admin_token(auth.authenticate_admin("root", PASSWORD))
    assert auth.authenticate_admin("root", "nope nope nope") is None


def test_routes_require_the_admin_pair_and_manage_accounts(monkeypatch):
    from lenny.app import app
    from lenny.core import auth
    monkeypatch.setattr(auth, "ADMIN_INTERNAL_SECRET", "s3cret")
    make()
    client = TestClient(app)
    headers = {"X-Admin-Internal-Secret": "s3cret",
               "Authorization": f"Bearer {auth.issue_admin_token()}"}
    assert client.get("/v1/api/admin/librarians").status_code == 403

    created = client.post("/v1/api/admin/librarians", headers=headers, json={
        "username": "ann", "email": "ann@example.org", "password": PASSWORD})
    assert created.status_code == 201 and "password" not in created.text
    assert client.post("/v1/api/admin/librarians", headers=headers, json={
        "username": "ann", "email": "x@example.org", "password": PASSWORD}).status_code == 400

    names = [l["username"] for l in client.get("/v1/api/admin/librarians", headers=headers).json()["librarians"]]
    assert names == ["root", "ann"]
    ann = created.json()["id"]
    assert client.post(f"/v1/api/admin/librarians/{ann}/disable", headers=headers,
                       json={"disabled": True}).json()["disabled"] is True
