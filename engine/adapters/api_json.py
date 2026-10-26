"""JSON API adapters: Hacker News search, GitHub search, and generic mappings."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx

from engine.http import get_json
from engine.models import Candidate

# HN Algolia search returns {hits: [{title, url, author, created_at, points, objectID, story_text}]}
HN_SEARCH = "https://hn.algolia.com/api/v1/search?query={query}&tags=show_hn&hitsPerPage=50"

# GitHub code/repo search: {items: [{html_url, name, description, owner, created_at, homepage, topics, stargazers_count}]}
GITHUB_SEARCH = "https://api.github.com/search/repositories?q={query}&sort=updated&per_page=50"

_MARKUP = re.compile(r"<[^>]+>")


def _strip(text: str | None) -> str | None:
    if not text:
        return None
    return _MARKUP.sub("", text).strip()[:4000] or None


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(UTC)
    except ValueError:
        return None


class ApiJsonAdapter:
    type = "api_json"

    def __init__(self, source: dict):
        self.source = source
        self.preset = source.get("preset", "generic")

    def fetch(self, url: str, since) -> Iterable[Candidate]:
        if self.preset == "hn_search":
            yield from self._hn_search(since)
        elif self.preset == "github_search":
            yield from self._github_search(since)
        else:
            yield from self._generic(url, since)

    # -- Hacker News ---------------------------------------------------------
    def _hn_search(self, since) -> Iterable[Candidate]:
        queries = self.source.get("query", [])
        if isinstance(queries, str):
            queries = [queries]
        min_points = int((self.source.get("filters") or {}).get("min_points", 0))
        with httpx.Client() as client:
            for q in queries:
                data = get_json(HN_SEARCH.format(query=httpx.QueryParams({"q": q}).get("q")), client)
                if not data:
                    continue
                for hit in data.get("hits", []):
                    # Text-only posts have no app URL; they used to become
                    # candidates pointing at the HN thread itself.
                    link = hit.get("url")
                    if not link or (hit.get("points") or 0) < min_points:
                        continue
                    yield Candidate(
                        url=link,
                        title=hit.get("title", "") or "",
                        text=_strip(hit.get("story_text") or hit.get("comment_text")),
                        creator=hit.get("author"),
                        published=_date(hit.get("created_at")),
                        source_id=self.source["id"],
                        raw={"points": hit.get("points", 0), "hn_id": hit.get("objectID"), "query": q},
                    )

    # -- GitHub ---------------------------------------------------------------
    def _github_search(self, since) -> Iterable[Candidate]:
        queries = self.source.get("query", [])
        if isinstance(queries, str):
            queries = [queries]
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with httpx.Client() as client:
            for q in queries:
                data = get_json(GITHUB_SEARCH.format(query=httpx.QueryParams({"q": q}).get("q")), client, headers)
                if not data:
                    continue
                for item in data.get("items", []):
                    homepage = item.get("homepage")
                    link = homepage if homepage and homepage.startswith("http") else item.get("html_url")
                    if not link:
                        continue
                    yield Candidate(
                        url=link,
                        title=item.get("name", "") or "",
                        text=_strip(item.get("description")),
                        creator=(item.get("owner") or {}).get("login"),
                        published=_date(item.get("created_at")),
                        source_id=self.source["id"],
                        raw={
                            "stars": item.get("stargazers_count", 0),
                            "topics": item.get("topics", []),
                            "repo_url": item.get("html_url"),
                            "is_repo": link == item.get("html_url"),
                        },
                    )

    # -- Generic JSON API with mapping rules -----------------------------------
    def _generic(self, url: str, since) -> Iterable[Candidate]:
        mapping = self.source.get("mapping", {})
        with httpx.Client() as client:
            data = get_json(url, client)
        if data is None:
            return
        rows = data
        if "list_path" in mapping:
            for key in mapping["list_path"].split("."):
                rows = (rows or {}).get(key, []) if isinstance(rows, dict) else rows
        # Optional listing-level facts from sources.yml: where the list lives (the
        # "found on" link) and a sentence quoted from it, e.g. the creator saying how
        # the tools were made. The note reaches the classifier as source text.
        note = self.source.get("listing_note", "")
        listing = self.source.get("listing_url", "")
        for row in rows or []:
            link = self._map(mapping.get("url", "url"), row)
            if not link:
                continue
            text = self._map(mapping.get("text"), row) or ""
            yield Candidate(
                url=urljoin(url, link),  # lists often hold site-relative paths
                title=self._map(mapping.get("title", "title"), row) or "",
                text=f"{text}\n\n{note}".strip() if note else (text or None),
                creator=self._map(mapping.get("creator"), row),
                published=_date(self._map(mapping.get("published"), row)),
                source_id=self.source["id"],
                raw={**row, **({"listing_url": listing} if listing else {})},
            )

    @staticmethod
    def _map(rule, row: dict) -> str | None:
        if not rule:
            return None
        val = row
        for part in rule.split("."):
            val = val.get(part) if isinstance(val, dict) else None
            if val is None:
                break
        return str(val) if val is not None else None
