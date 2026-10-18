"""Librarians (#234): admin accounts in the database, hashed, with a protected super user."""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("TESTING", "true")

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_table():
    from lenny.core.db import Base, engine, session
    from lenny.core import librarians, cache  # noqa: F401
    Base.metadata.create_all(engine)
    session.remove()
    yield
    session.remove()
    with engine.begin() as c:
        c.exec_driver_sql("delete from librarians")
        c.exec_driver_sql("delete from cache")


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
        librarians.create("Other", "other@example.org", PASSWORD, username="root")
    with pytest.raises(librarians.LibrarianError):
        librarians.create("Other", "ROOT@example.org", PASSWORD)


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


@pytest.fixture
def api(monkeypatch):
    from lenny.app import app
    from lenny.core import auth
    monkeypatch.setattr(auth, "ADMIN_INTERNAL_SECRET", "s3cret")
    client = TestClient(app)

    def call(method, path, row=None, **kw):
        headers = {"X-Admin-Internal-Secret": "s3cret"}
        headers["Authorization"] = "Bearer " + (auth.issue_admin_token(row.id) if row else auth.issue_admin_token())
        return client.request(method, "/v1/api" + path, headers=headers, **kw)
    return call


def test_routes_need_the_admin_pair_and_manage_accounts(api):
    from fastapi.testclient import TestClient
    from lenny.app import app
    root = make()
    assert TestClient(app).get("/v1/api/admin/librarians").status_code == 403

    created = api("POST", "/admin/librarians", root, json={
        "name": "Ann", "email": "ann@example.org", "password": PASSWORD})
    assert created.status_code == 201 and "password" not in created.text
    assert api("POST", "/admin/librarians", root, json={
        "name": "Ann", "email": "ann@example.org", "password": PASSWORD}).status_code == 400
    names = [l["username"] for l in api("GET", "/admin/librarians", root).json()["librarians"]]
    assert names == ["root", "ann"]
    assert api("POST", f"/admin/librarians/{created.json()['id']}/disable", root,
               json={"disabled": True}).json()["disabled"] is True


def test_a_non_super_librarian_sees_and_edits_only_themselves(api):
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    assert api("GET", "/admin/me", ann).json()["username"] == "ann"
    assert api("GET", "/admin/librarians", ann).status_code == 403
    assert api("POST", "/admin/librarians", ann, json={
        "name": "B", "email": "b@example.org", "password": PASSWORD}).status_code == 403
    assert api("POST", f"/admin/librarians/{root.id}/disable", ann, json={}).status_code == 403
    assert api("PUT", f"/admin/librarians/{root.id}", ann, json={"email": "x@example.org"}).status_code == 403
    assert api("PUT", f"/admin/librarians/{ann.id}", ann, json={"email": "new@example.org"}).status_code == 200
    # nobody promotes themselves
    assert api("PUT", f"/admin/librarians/{ann.id}", ann, json={"is_super": True}).status_code == 403


def test_changing_your_own_password_needs_the_current_one(api):
    from lenny.core import librarians
    ann = make("ann", "ann@example.org", is_super=False)
    new = "a brand new passphrase"
    assert api("PUT", f"/admin/librarians/{ann.id}", ann, json={"password": new}).status_code == 400
    assert api("PUT", f"/admin/librarians/{ann.id}", ann,
               json={"password": new, "current_password": "wrong password!!"}).status_code == 400
    assert api("PUT", f"/admin/librarians/{ann.id}", ann,
               json={"password": new, "current_password": PASSWORD}).status_code == 200
    assert librarians.authenticate("ann", new)


def test_super_user_resets_another_password_without_the_old_one(api):
    from lenny.core import librarians
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    assert api("PUT", f"/admin/librarians/{ann.id}", root, json={"password": "reset by the super user"}).status_code == 200
    assert librarians.authenticate("ann", "reset by the super user")


def test_making_someone_super_hands_the_role_over(api):
    from lenny.core import librarians
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    # nobody changes their own super status
    r = api("PUT", f"/admin/librarians/{root.id}", root, json={"is_super": False, "current_password": PASSWORD})
    assert r.status_code == 400 and r.json()["detail"] == "You cannot change your own super status."
    r = api("PUT", f"/admin/librarians/{ann.id}", root, json={"is_super": True, "current_password": PASSWORD})
    assert r.status_code == 200 and r.json()["transferred"] is True
    librarians.db.expire_all()  # the route ran in its own session
    assert librarians.get(ann.id).is_super and not librarians.get(root.id).is_super
    # the old super user is a normal member at once
    assert api("GET", "/admin/librarians", root).status_code == 403
    assert api("GET", "/admin/librarians", ann).status_code == 200


def test_there_is_always_exactly_one_super_user(api):
    from lenny.core import librarians
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    # the super user cannot be demoted or disabled, only replaced
    with pytest.raises(librarians.LibrarianError):
        librarians.update(root.id, is_super=False)
    with pytest.raises(librarians.LibrarianError):
        librarians.set_disabled(root.id, True)
    assert api("POST", f"/admin/librarians/{root.id}/disable", ann, json={}).status_code == 403
    # a disabled account cannot take the role
    librarians.set_disabled(ann.id, True)
    with pytest.raises(librarians.LibrarianError):
        librarians.update(ann.id, is_super=True)
    # creating a super user also replaces the current one, and old data with two collapses to one
    librarians.set_disabled(ann.id, False)
    bob = librarians.create("Bob", "bob@example.org", PASSWORD, is_super=True)
    assert [r.username for r in librarians.list_all() if r.is_super] == [bob.username]


def test_making_someone_super_needs_your_own_password(api):
    from lenny.core import librarians
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    for body in ({"is_super": True}, {"is_super": True, "current_password": "wrong password!!"}):
        assert api("PUT", f"/admin/librarians/{ann.id}", root, json=body).status_code == 400
    assert not librarians.get(ann.id).is_super
    # nor can a new super user be created with a live session alone
    new = {"name": "Bob", "email": "bob@example.org", "password": PASSWORD, "is_super": True}
    assert api("POST", "/admin/librarians", root, json=new).status_code == 400
    assert api("POST", "/admin/librarians", root, json={**new, "current_password": PASSWORD}).status_code == 201


def test_disabling_a_librarian_ends_their_session_at_once(api):
    from lenny.core import auth, librarians
    root = make()
    ann = make("ann", "ann@example.org", is_super=False)
    token = auth.issue_admin_token(ann.id)
    assert auth.verify_admin_token(token)
    librarians.set_disabled(ann.id, True)
    assert not auth.verify_admin_token(token)
    assert api("GET", "/admin/me", ann).status_code == 401


def test_tokens_without_a_named_librarian_are_refused_except_the_internal_one(api):
    from lenny.core import auth
    legacy = auth._get_admin_serializer().dumps({"admin": True})
    assert not auth.verify_admin_token(legacy)
    assert auth.verify_admin_token(auth.issue_admin_token())
    # the process's own token reaches admin routes but is no one's account
    assert api("GET", "/admin/me").status_code == 403


def test_login_reports_who_signed_in(api):
    from lenny.app import app
    make()
    r = TestClient(app).post("/v1/api/admin/auth", headers={"X-Admin-Internal-Secret": "s3cret"},
                             json={"username": "root", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["username"] == "root" and r.json()["is_super"] is True


def test_username_is_derived_from_the_email_and_made_unique():
    from lenny.core import librarians
    a = librarians.create("Ada Lovelace", "Ada.L+x@Example.org", PASSWORD)
    b = librarians.create("Ada Two", "ada.l@other.org", PASSWORD)
    assert a.username == "ada.l+x".replace("+x", "x") and b.username == "ada.l"
    c = librarians.create("Ada Three", "ada.l@third.org", PASSWORD)
    assert c.username == "ada.l2"


def test_name_is_what_people_see_and_falls_back_to_the_username():
    from lenny.core import librarians
    from lenny.core.db import session
    named = make("  Ada   Lovelace ")
    assert named.to_dict()["name"] == "Ada Lovelace" and named.to_dict()["display_name"] == "Ada Lovelace"
    legacy = librarians.Librarian(username="admin", password_hash="x", is_super=True)
    session.add(legacy); session.commit()
    assert legacy.to_dict()["name"] is None and legacy.to_dict()["display_name"] == "admin"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, "bad\x00name", None, 7, ["a"]])
def test_a_bad_name_is_refused(name):
    from lenny.core import librarians
    with pytest.raises(librarians.LibrarianError, match="name"):
        librarians.create(name, "n@example.org", PASSWORD)


def test_sign_in_works_with_the_email_or_the_username(api):
    from fastapi.testclient import TestClient
    from lenny.app import app
    from lenny.core import librarians
    make("Root Person", "Root@Example.org")
    assert librarians.authenticate("root", PASSWORD) and librarians.authenticate("ROOT@example.org", PASSWORD)
    r = TestClient(app).post("/v1/api/admin/auth", headers={"X-Admin-Internal-Secret": "s3cret"},
                             json={"username": "root@example.org", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["display_name"] == "Root Person"


def test_a_librarian_edits_their_own_name_and_the_super_user_anyones(api):
    root = make()
    ann = make("Ann", "ann@example.org", is_super=False)
    assert api("PUT", f"/admin/librarians/{ann.id}", ann, json={"name": "Ann B"}).json()["display_name"] == "Ann B"
    assert api("PUT", f"/admin/librarians/{ann.id}", root, json={"name": "Annie"}).json()["name"] == "Annie"
    assert api("PUT", f"/admin/librarians/{root.id}", ann, json={"name": "Boss"}).status_code == 403
    assert api("PUT", f"/admin/librarians/{ann.id}", ann, json={"name": ""}).status_code == 400


def test_create_returns_the_derived_username(api):
    root = make()
    r = api("POST", "/admin/librarians", root, json={"name": "Bea Smith", "email": "bea@example.org", "password": PASSWORD})
    assert r.status_code == 201 and r.json()["username"] == "bea" and r.json()["display_name"] == "Bea Smith"
    dup = api("POST", "/admin/librarians", root, json={"name": "Bea Two", "email": "bea@example.org", "password": PASSWORD})
    assert dup.status_code == 400 and dup.json()["detail"] == "That email is already in use."


def test_failed_guesses_by_email_and_username_share_one_counter():
    from lenny.core import librarians
    make()
    for who in ["root", "root@example.org"] * 5:
        assert librarians.authenticate(who, "wrong password!!") is None
    with pytest.raises(librarians.TooManyAttempts):
        librarians.authenticate("root@example.org", PASSWORD)


def test_a_failed_login_says_the_same_for_unknown_wrong_and_disabled(api):
    from fastapi.testclient import TestClient
    from lenny.app import app
    from lenny.core import librarians
    make()
    gone = make("Gone", "gone@example.org", is_super=False)
    librarians.set_disabled(gone.id, True)
    c, h = TestClient(app), {"X-Admin-Internal-Secret": "s3cret"}
    seen = {(r.status_code, r.json()["detail"]) for r in (
        c.post("/v1/api/admin/auth", headers=h, json={"username": u, "password": p})
        for u, p in [("nobody", PASSWORD), ("root", "wrong password!!"), ("gone@example.org", PASSWORD)])}
    assert seen == {(401, "Invalid email, username or password.")}


def test_logout_ends_every_session_of_that_librarian_only(api):
    from lenny.core import auth
    root = make()
    ann = make("Ann", "ann@example.org", is_super=False)
    t1, t2, other = auth.issue_admin_token(ann.id), auth.issue_admin_token(ann.id), auth.issue_admin_token(root.id)
    assert auth.verify_admin_token(t1) and auth.verify_admin_token(t2)
    assert api("POST", "/admin/logout", ann).status_code == 200
    assert not auth.verify_admin_token(t1) and not auth.verify_admin_token(t2)
    assert auth.verify_admin_token(other)
    assert auth.verify_admin_token(auth.issue_admin_token(ann.id))  # signing in again works
    assert api("POST", "/admin/logout").status_code == 403  # the process's own token names no one


def test_a_long_email_can_sign_in_and_old_hashes_are_upgraded():
    from argon2 import PasswordHasher
    from lenny.core import librarians
    email = "a" * 70 + "@example.org"
    row = librarians.create("Long Address", email, PASSWORD)
    assert librarians.authenticate(email, PASSWORD)
    row.password_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    old = row.password_hash
    assert librarians.authenticate(email, PASSWORD)
    assert librarians.get(row.id).password_hash != old and librarians.authenticate(email, PASSWORD)
