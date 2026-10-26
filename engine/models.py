"""Shared data types for the engine pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Candidate:
    """A normalised discovery result produced by a source adapter."""

    url: str
    title: str
    text: str | None = None
    creator: str | None = None
    published: datetime | None = None
    source_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Record:
    """A candidate promoted to a full item record as it moves through stages."""

    id: str
    url: str
    source_id: str
    title: str = ""
    summary: str = ""
    why: str = ""
    creator: dict | None = None
    image: dict | None = None
    category: str = ""
    subcategory: str = ""
    tags: list[str] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)
    dates: dict[str, str] = field(default_factory=dict)
    status: str = "active"
    provenance: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    ai_claim: str = "none"
    quality: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    text: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    tier: str = "B"
    mentions: int = 1
    liveness: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_json(self, include_text: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id,
            "url": self.url,
            "source": self.source_id,
            "tier": self.tier,
            "title": self.title,
            "summary": self.summary,
            "why": self.why,
            "category": self.category,
            "subcategory": self.subcategory,
            "tags": self.tags,
            "attrs": self.attrs,
            "dates": self.dates,
            "status": self.status,
            "provenance": self.provenance,
            "evidence": self.evidence,
            "ai_claim": self.ai_claim,
            "quality": self.quality,
            "flags": self.flags,
        }
        if self.creator:
            d["creator"] = self.creator
        if self.image:
            d["image"] = self.image
        if include_text:
            # Page text and raw source payloads are kept on review records so
            # `reclassify` can re-score without refetching the web.
            d["text"] = self.text
            d["raw"] = self.raw
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Record:
        """Rebuild a full Record from a stored item/review file. Every field the
        record carried at save time must survive the round trip — dropping any of
        them (as an earlier reclassify did with dates/provenance) silently
        destroys data."""
        return cls(
            id=d.get("id", ""),
            url=d.get("url", ""),
            source_id=d.get("source", d.get("source_id", "")),
            tier=d.get("tier", (d.get("raw") or {}).get("source_tier", "B")),
            title=d.get("title", ""),
            summary=d.get("summary", ""),
            why=d.get("why", ""),
            creator=d.get("creator"),
            image=d.get("image"),
            category=d.get("category", ""),
            subcategory=d.get("subcategory", ""),
            tags=list(d.get("tags") or []),
            attrs=dict(d.get("attrs") or {}),
            dates=dict(d.get("dates") or {}),
            status=d.get("status", "active"),
            provenance=list(d.get("provenance") or []),
            evidence=list(d.get("evidence") or []),
            ai_claim=d.get("ai_claim", "none"),
            quality=dict(d.get("quality") or {}),
            flags=list(d.get("flags") or []),
            text=d.get("text"),
            raw=dict(d.get("raw") or {}),
            liveness=dict(d.get("liveness") or {}),
        )
