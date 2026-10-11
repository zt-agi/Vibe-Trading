"""Read-only VT route for the public Damnang Research article index.

The publisher's JSON is metadata, not article text or market data. The route
keeps a short in-memory cache and never writes source material to the project.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from starlette.routing import Mount

SOURCE_URL = "https://map.damnang.com/data/posts.json"
ROUTE_PATH = "/zt/damnang/api/posts"
MAX_BYTES = 1_000_000
CACHE_SECONDS = 600
_lock = threading.Lock()
_cache: dict[str, Any] = {"posts": None, "loaded_at": 0.0, "retrieved_at": None}


def _text(value: Any, limit: int = 500) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _words(value: Any, limit: int = 16) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, 80) for item in value[:limit] if _text(item, 80)]


def _post(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    url = _text(raw.get("url"), 1000)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"damnang.com", "www.damnang.com"} or not parsed.path.startswith("/p/"):
        return None
    title = _text(raw.get("title"), 300)
    if not title:
        return None
    audience = raw.get("audience")
    post_type = raw.get("type")
    return {
        "id": _text(raw.get("id"), 200) or parsed.path.rsplit("/", 1)[-1],
        "title": title,
        "subtitle": _text(raw.get("subtitle"), 500),
        "published_at": _text(raw.get("publishedAt"), 40),
        "date": _text(raw.get("date"), 20),
        "audience": audience if audience in {"free", "paid"} else "unknown",
        "type": post_type if post_type in {"essay", "call"} else "other",
        "url": url,
        "topics": _words(raw.get("topics")),
        "tickers": _words(raw.get("tickers")),
        "bottleneck": _text(raw.get("bottleneck"), 1000),
        "verified_at": _text(raw.get("verifiedAt"), 50),
    }


def _fetch() -> list[dict[str, Any]]:
    request = Request(SOURCE_URL, headers={"Accept": "application/json", "User-Agent": "Vibe-Trading-Damnang-Index/1.0"})
    with urlopen(request, timeout=12) as response:
        if urlparse(response.geturl()).netloc.lower() != "map.damnang.com":
            raise ValueError("publisher index redirected to another host")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("publisher index is too large")
    raw = json.loads(data)
    if not isinstance(raw, list):
        raise ValueError("publisher index is not a list")
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw:
        post = _post(row)
        if post and post["url"] not in seen:
            seen.add(post["url"])
            posts.append(post)
    if not posts:
        raise ValueError("publisher index contains no usable article links")
    return posts


def article_index(refresh: bool = False) -> dict[str, Any]:
    """Return current public metadata, or explicitly marked stale cache."""
    with _lock:
        age = time.monotonic() - _cache["loaded_at"]
        if _cache["posts"] is not None and not refresh and age < CACHE_SECONDS:
            status = "cached"
        else:
            try:
                posts = _fetch()
            except Exception as exc:
                if _cache["posts"] is None:
                    raise RuntimeError("Publisher article index is unavailable") from exc
                status = "stale"
            else:
                _cache.update(posts=posts, loaded_at=time.monotonic(),
                              retrieved_at=datetime.now(timezone.utc).isoformat())
                status = "live"
        posts = _cache["posts"]
        return {
            "source": "Damnang Research public article index",
            "source_url": SOURCE_URL,
            "retrieved_at": _cache["retrieved_at"],
            "status": status,
            "count": len(posts),
            "articles": posts,
            "authority": "PUBLISHER_METADATA_ONLY_NON_PIT",
        }


def get_posts(refresh: bool = False) -> JSONResponse:
    try:
        payload = article_index(refresh)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def register(app: Any) -> list[Any]:
    """Register before VT's catch-all SPA mount, preserving VT auth."""
    if any(getattr(route, "path", None) == ROUTE_PATH for route in app.router.routes):
        return []
    from src.api.security import require_auth

    before = list(app.router.routes)
    app.add_api_route(ROUTE_PATH, get_posts, methods=["GET"],
                      dependencies=[Depends(require_auth)], response_model=None,
                      tags=["zt-damnang"], name="zt_damnang_posts")
    added = [route for route in app.router.routes if route not in before]
    routes = app.router.routes
    mount = next((route for route in routes if isinstance(route, Mount) and route.path in {"", "/"}), None)
    if mount is not None:
        rest = [route for route in routes if route not in added]
        index = rest.index(mount)
        routes[:] = rest[:index] + added + rest[index:]
    return added
