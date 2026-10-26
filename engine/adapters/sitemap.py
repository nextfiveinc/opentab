"""Sitemap adapter: discovers URLs from a sitemap.xml (optionally filtered)."""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from engine.http import fetch
from engine.models import Candidate


class SitemapAdapter:
    type = "sitemap"

    def __init__(self, source: dict):
        self.source = source
        self.include = source.get("filters", {}).get("include", [])
        self.exclude = source.get("filters", {}).get("exclude", [])

    def fetch(self, url: str, since) -> Iterable[Candidate]:
        with httpx.Client() as client:
            result = fetch(url, client)
            if not result.ok:
                return
            import re
            locs = re.findall(r"<loc>([^<]+)</loc>", result.body or "")
            for loc in locs:
                if loc.endswith(".xml"):  # sitemap index: recurse one level
                    sub = fetch(loc, client)
                    if sub.ok:
                        locs.extend(re.findall(r"<loc>([^<]+)</loc>", sub.body or ""))
                    continue
                if self.include and not any(p in loc for p in self.include):
                    continue
                if any(p in loc for p in self.exclude):
                    continue
                yield Candidate(
                    url=loc,
                    title=loc.rsplit("/", 1)[-1].replace("-", " ").strip(),
                    text=None,
                    creator=None,
                    published=None,
                    source_id=self.source["id"],
                    raw={"source_type": "sitemap"},
                )
