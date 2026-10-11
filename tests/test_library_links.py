"""Share links for the library: the OPDS feed, a per-book OPDS address and the Book Server reader link."""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("TESTING", "true")
SECRET = "internal-secret-for-tests"
URL = "/v1/api/admin/library/links"


@pytest.fixture(scope="module")
def app_module():
    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield app


@pytest.fixture
def headers():
    from lenny.core import auth
    with patch.object(auth, "ADMIN_INTERNAL_SECRET", SECRET), patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None):
        yield {"X-Admin-Internal-Secret": SECRET, "Authorization": f"Bearer {auth.issue_admin_token()}"}


def links(app_module, headers, extra=None, base="http://testserver"):
    from fastapi.testclient import TestClient
    return TestClient(app_module, base_url=base, headers={**headers, **(extra or {})}).get(URL)


def test_links_come_from_the_address_the_admin_used(app_module, headers):
    r = links(app_module, headers, base="https://library.example.org")
    body = r.json()
    assert r.status_code == 200 and body["source"] == "request" and body["is_public"] is True
    assert body["base_url"] == "https://library.example.org"
    assert body["opds_feed"] == "https://library.example.org/v1/api/opds"
    assert body["opds_book_template"] == "https://library.example.org/v1/api/opds/{olid}"
    assert body["opds_search"] == "https://library.example.org/v1/api/opds/search?query={query}"
    assert body["reader_url"] == "https://reader.archive.org/catalog?home=https%3A%2F%2Flibrary.example.org%2Fv1%2Fapi%2Fopds"


def test_a_proxy_in_front_is_followed_and_a_configured_address_wins(app_module, headers):
    body = links(app_module, headers, {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "tunnel.trycloudflare.com"}).json()
    assert body["base_url"] == "https://tunnel.trycloudflare.com" and body["is_public"] is True
    from lenny import configs
    with patch.object(configs, "PUBLIC_URL", "https://mine.example.org"):
        body = links(app_module, headers, {"X-Forwarded-Host": "other.example.org"}).json()
    assert body["base_url"] == "https://mine.example.org" and body["source"] == "configured"
    assert body["opds_feed"] == "https://mine.example.org/v1/api/opds"


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080", "192.168.1.20", "10.0.0.5:1337", "raspberrypi.local", "lenny", "[::1]:8080"])
def test_addresses_that_only_work_on_this_network_are_flagged(app_module, headers, host):
    assert links(app_module, headers, {"X-Forwarded-Host": host}).json()["is_public"] is False


@pytest.mark.parametrize("host", ["library.example.org", "8.8.8.8", "my-lib.trycloudflare.com:443"])
def test_public_addresses_are_not_flagged(app_module, headers, host):
    assert links(app_module, headers, {"X-Forwarded-Host": host}).json()["is_public"] is True


@pytest.mark.parametrize("host", ["evil.com/path", "a b", "user@evil.com", "x?y", ""])
def test_a_hostile_forwarded_host_cannot_inject_into_the_links(app_module, headers, host):
    body = links(app_module, headers, {"X-Forwarded-Host": host}).json()
    assert body["base_url"].count("/") == 2 and "@" not in body["base_url"] and " " not in body["base_url"] and "?" not in body["base_url"]


def test_a_junk_forwarded_scheme_falls_back(app_module, headers):
    body = links(app_module, headers, {"X-Forwarded-Proto": "javascript", "X-Forwarded-Host": "library.example.org"}).json()
    assert body["base_url"] == "http://localhost"


def test_the_public_url_setting_is_cleaned():
    import importlib
    from lenny import configs
    with patch.dict(os.environ, {"LENNY_PUBLIC_URL": " https://a.example.org/// "}):
        importlib.reload(configs)
        assert configs.PUBLIC_URL == "https://a.example.org"
    with patch.dict(os.environ, {"LENNY_PUBLIC_URL": "  "}):
        importlib.reload(configs)
        assert configs.PUBLIC_URL is None
    importlib.reload(configs)


def test_links_require_admin(app_module):
    from fastapi.testclient import TestClient
    assert TestClient(app_module).get(URL).status_code == 403
