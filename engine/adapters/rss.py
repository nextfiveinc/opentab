"""RSS / Atom / JSON Feed adapter."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import feedparser

from engine.models import Candidate


def _parse_date(x) -> datetime | None:
    if not x:
        return None
    try:
        return datetime(*x[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class RssAdapter:
    type = "rss"

    def __init__(self, source: dict):
        self.source = source

    def fetch(self, url: str, since) -> Iterable[Candidate]:
        d = feedparser.parse(url)
        for e in d.entries:
            pub = _parse_date(getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None))
            link = getattr(e, "link", "")
            if not link:
                continue
            if since and pub and pub <= since:
                continue
            yield Candidate(
                url=link,
                title=getattr(e, "title", "") or "",
                text=(getattr(e, "summary", "") or "")[:4000],
                creator=(getattr(e, "author", "") or None),
                published=pub,
                source_id=self.source["id"],
                raw={"source_type": "rss"},
            )
