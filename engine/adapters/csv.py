"""CSV adapter: a CSV file or a Google Sheet published as CSV (approved tips)."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from engine.models import Candidate


class CsvAdapter:
    type = "csv"

    def __init__(self, source: dict):
        self.source = source

    def fetch(self, url: str, since) -> Iterable[Candidate]:
        text = self._read(url)
        if not text:
            return
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            link = (row.get("url") or row.get("link") or "").strip()
            if not link:
                continue
            approved = (row.get("approved") or "").strip().lower()
            if approved and approved not in ("yes", "true", "approved", "y", "1"):
                continue
            pub = self._date(row.get("submitted_on") or row.get("published") or row.get("date"))
            if since and pub and pub <= since:
                continue
            yield Candidate(
                url=link,
                title=(row.get("title") or "").strip(),
                text=(row.get("notes") or row.get("description") or "").strip()[:4000],
                creator=(row.get("submitter") or row.get("name") or "").strip() or None,
                published=pub,
                source_id=self.source["id"],
                raw={"evidence_url": row.get("evidence_url", ""), "built_with": row.get("built_with", "")},
            )

    def _read(self, url: str) -> str | None:
        if url.startswith("file://"):
            from pathlib import Path
            return Path(url[7:]).read_text(encoding="utf-8")
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True)
        except httpx.HTTPError:
            return None
        return r.text if r.status_code == 200 else None

    @staticmethod
    def _date(s) -> datetime | None:
        if not s:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(s.strip(), fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        return None
