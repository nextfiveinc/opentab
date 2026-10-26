"""Shared HTTP client: polite fetching with conditional requests and per-host limits."""

from __future__ import annotations

import json
import time
import urllib.robotparser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

USER_AGENT = "OpenTab-Catalogue/0.1 (+https://github.com/opentab/catalogue; engine bot)"
DEFAULT_TIMEOUT = 20.0
_MIN_DELAY = 1.5  # seconds between requests to the same host

_last_hit: dict[str, float] = {}
_conditional: dict[str, dict[str, str]] = {}


class FetchResult:
    def __init__(self, status: int, body: str | None = None, headers: dict | None = None,
                 error: str | None = None, url: str = ""):
        self.url = url  # final URL after redirects (drift and sign-in checks use it)
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and self.body is not None


def load_conditional_cache(path: Path) -> None:
    global _conditional
    if path.exists():
        try:
            _conditional = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _conditional = {}


def save_conditional_cache(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_conditional, ensure_ascii=False, indent=1), encoding="utf-8")


def _polite_wait(host: str) -> None:
    now = time.monotonic()
    last = _last_hit.get(host, 0.0)
    wait = _MIN_DELAY - (now - last)
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _robots_for(url: str, client: httpx.Client) -> urllib.robotparser.RobotFileParser | None:
    """robots.txt per host, fetched once and cached for the process."""
    parts = urlsplit(url)
    host = f"{parts.scheme}://{parts.netloc}"
    if host in _robots:
        return _robots[host]
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = client.get(f"{host}/robots.txt", headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        if r.status_code == 200:
            rp.parse(r.text.splitlines())
            _robots[host] = rp
        else:
            _robots[host] = None  # no robots.txt: allow
    except httpx.HTTPError:
        _robots[host] = None
    return _robots[host]


def allowed_by_robots(url: str, client: httpx.Client) -> bool:
    rp = _robots_for(url, client)
    if rp is None:
        return True
    return rp.can_fetch(USER_AGENT, url)


def fetch(url: str, client: httpx.Client, conditional: bool = False,
          respect_robots: bool = True) -> FetchResult:
    """GET with a descriptive UA, per-host pacing and ETag/Last-Modified revalidation."""
    host = urlsplit(url).netloc
    if respect_robots and not allowed_by_robots(url, client):
        return FetchResult(403, error="blocked-by-robots")
    _polite_wait(host)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8"}
    if conditional:
        saved = _conditional.get(url, {})
        if saved.get("etag"):
            headers["If-None-Match"] = saved["etag"]
        if saved.get("last_modified"):
            headers["If-Modified-Since"] = saved["last_modified"]
    try:
        r = client.get(url, headers=headers, follow_redirects=True, timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        return FetchResult(0, error=type(e).__name__)
    if conditional and r.status_code == 200:
        _conditional[url] = {
            "etag": r.headers.get("etag", ""),
            "last_modified": r.headers.get("last-modified", ""),
        }
    return FetchResult(r.status_code, r.text if r.status_code == 200 else None, dict(r.headers),
                       url=str(r.url))


def post_json(url: str, client: httpx.Client, payload: dict, headers: dict | None = None) -> Any:
    r = client.post(url, json=payload, headers=headers, timeout=60.0)
    if r.status_code >= 400:
        raise RuntimeError(f"POST {url} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def get_json(url: str, client: httpx.Client, headers: dict | None = None) -> Any | None:
    try:
        r = client.get(url, headers=headers, timeout=30.0, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None
