"""Manual adapter: a hand-edited YAML list of URLs (data/manual_sources.yml)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from engine.models import Candidate


class ManualAdapter:
    """Reads a list like:

    items:
      - url: https://example.app/
        title: Example App
        notes: Submitted by a friend
        evidence_url: https://example.com/post
        built_with: claude
        found_on: https://example.com/list   # optional: the "found on" link (else the app)
    """

    type = "manual"

    def __init__(self, source: dict):
        self.source = source

    def fetch(self, url: str, since) -> Iterable[Candidate]:
        path = Path(url or self.source.get("file", "data/manual_sources.yml"))
        if not path.exists():
            return
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for row in data.get("items", []):
            if not row.get("url"):
                continue
            yield Candidate(
                url=row["url"],
                title=row.get("title", ""),
                text=row.get("notes"),
                creator=row.get("creator"),
                published=None,
                source_id=self.source["id"],
                raw={
                    "evidence_url": row.get("evidence_url", ""),
                    "built_with": row.get("built_with", ""),
                    "manual": True,
                    **({"listing_url": row["found_on"]} if row.get("found_on") else {}),
                },
            )
