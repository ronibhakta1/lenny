"""A patron with book A on loan must not be able to steer the readium proxy at book B."""

import base64
import os

import pytest

os.environ.setdefault("TESTING", "true")

from fastapi.testclient import TestClient  # noqa: E402

from tests.test_read_gate import PATRON, borrowed_item  # noqa: E402,F401


class _Fake:
    def __init__(self, urls):
        self.urls = urls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.urls.append(url)
        import httpx
        return httpx.Response(200, json={"links": [{"rel": "self", "href": "x"}]},
                              request=httpx.Request("GET", url))


@pytest.fixture
def fetched(monkeypatch):
    import httpx
    urls = []
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _Fake(urls))
    return urls


def _s3_key(url):
    b64 = url.split("/webpub/")[1].split("/")[0]
    return base64.b64decode(b64.replace("_", "/").replace("-", "+") + "==").decode()


def _client():
    from lenny.core import auth
    c = TestClient(app_(), follow_redirects=False)
    c.cookies.set("session", auth.create_session_cookie(PATRON))
    return c


def app_():
    from lenny.app import app
    return app


@pytest.mark.parametrize("fmt", ["23.epub", "/../99002.epub", "../x", "epub/y", ""])
def test_format_cannot_point_the_proxy_at_another_book(borrowed_item, fetched, fmt):
    c = _client()
    for path in ("/v1/api/items/99001/readium/manifest.json", "/v1/api/items/99001/readium/a/b.xhtml"):
        r = c.get(path, params={"format": fmt})
        assert r.status_code == 400, (path, fmt, r.status_code)
    assert fetched == []


@pytest.mark.parametrize("rest", ["%2e%2e/%2e%2e/abc/manifest.json", "a/%2e%2e/%2e%2e/b", "a/%2e/b", "a%5C..%5Cb", "a%3Fb"])
def test_readium_path_cannot_climb_out_of_the_book(borrowed_item, fetched, rest):
    r = _client().get(f"/v1/api/items/99001/readium/{rest}")
    assert r.status_code == 400 and fetched == []


def test_normal_readium_requests_still_reach_this_book_only(borrowed_item, fetched):
    c = _client()
    for fmt in ("epub", ".epub"):
        c.get("/v1/api/items/99001/readium/OEBPS/ch1.xhtml", params={"format": fmt})
    assert fetched and all(_s3_key(u) == "s3://bookshelf/99001.epub" or _s3_key(u) == "s3://bookshelf/99001epub"
                           for u in fetched)
