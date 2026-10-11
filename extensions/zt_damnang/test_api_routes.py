"""Source and failure-boundary checks for the public article index route."""
from __future__ import annotations

import json

from extensions.zt_damnang import api_routes as route


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return route.SOURCE_URL

    def read(self, _size):
        return json.dumps(self.payload).encode("utf-8")


def _post(url="https://www.damnang.com/p/hbm-article", title="HBM test"):
    return {"id": "hbm", "title": title, "url": url, "audience": "paid",
            "type": "essay", "tickers": ["MU"], "body": "private body must not pass through"}


def test_index_restricts_links_and_returns_metadata_only(monkeypatch):
    payload = [_post(), _post(), _post(url="https://evil.example/p/fake")]
    monkeypatch.setattr(route, "urlopen", lambda *_args, **_kwargs: _Response(payload))
    articles = route._fetch()
    assert len(articles) == 1
    assert articles[0]["url"] == "https://www.damnang.com/p/hbm-article"
    assert articles[0]["audience"] == "paid"
    assert "body" not in articles[0]


def test_failed_refresh_marks_existing_cache_stale(monkeypatch):
    monkeypatch.setitem(route._cache, "posts", [route._post(_post())])
    monkeypatch.setitem(route._cache, "retrieved_at", "2026-10-11T07:00:00Z")
    monkeypatch.setitem(route._cache, "loaded_at", 0.0)
    monkeypatch.setattr(route, "_fetch", lambda: (_ for _ in ()).throw(OSError("offline")))
    result = route.article_index(refresh=True)
    assert result["status"] == "stale"
    assert result["count"] == 1
    assert result["authority"] == "PUBLISHER_METADATA_ONLY_NON_PIT"
