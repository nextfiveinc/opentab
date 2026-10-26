"""Item store: one JSON file per item under data/items/, plus overrides and rejected log.

Three layers merge on load: scraped/enriched item file, then human overrides from
data/overrides.yml. Overrides always win, and the engine never writes overrides.yml.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any

import yaml

from engine.config import Pack, load_json, write_json
from engine.models import Record
from engine.util import url_id

# Fetch failures that say nothing about the app itself: connection errors and
# timeouts (reason carries the httpx exception name), rate limits, server errors.
# A 404/403 or a robots block is a real answer and stays decided.
_RETRYABLE_FETCH = re.compile(r"^fetch-failed:(?:[A-Za-z]\w*|429|5\d\d)$")
DEFAULT_RETRY_DAYS = 14


def is_retryable_rejection(reason: str) -> bool:
    return bool(_RETRYABLE_FETCH.match(reason or ""))


class Store:
    """Persistent store for one pack's data folder."""

    def __init__(self, pack: Pack, dry_run: bool = False):
        self.pack = pack
        # Dry runs must leave data/ untouched; qualify and gate log rejections
        # through this store, which used to append to rejected.jsonl anyway.
        self.dry_run = dry_run
        self.items_dir = pack.data_dir / "items"
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.overrides_path = pack.data_dir / "overrides.yml"
        self.rejected_path = pack.private_dir / "rejected.jsonl"
        self._overrides: dict[str, Any] | None = None
        self._cache: dict[str, dict[str, Any]] = {}
        self._rejected_logged: set[tuple[str, str]] | None = None
        self.retrying: int = 0  # URLs known_urls() released for a fetch retry

    # -- overrides ----------------------------------------------------------
    @property
    def overrides(self) -> dict[str, Any]:
        if self._overrides is None:
            self._overrides = self._load_overrides()
        return self._overrides

    def _load_overrides(self) -> dict[str, Any]:
        data = yaml.safe_load(self.overrides_path.read_text(encoding="utf-8")) \
            if self.overrides_path.exists() else None
        if not data:
            return {}
        # Two accepted shapes: {"<id>": {...}, ...} or {"items": {"<id>": {...}}}
        return data.get("items", data) if isinstance(data, dict) else {}

    def apply_overrides(self, item: dict[str, Any]) -> dict[str, Any]:
        ov = self.overrides.get(item["id"])
        if ov:
            item.update(ov)
        return item

    # -- items --------------------------------------------------------------
    def load_items(self) -> dict[str, dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        if not self.items_dir.exists():
            return items
        for f in sorted(self.items_dir.glob("*.json")):
            try:
                item = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            item = self.apply_overrides(item)
            items[item["id"]] = item
        return items

    def load_item(self, item_id: str, apply_overrides: bool = True) -> dict[str, Any] | None:
        if item_id not in self._cache:
            f = self.items_dir / f"{item_id}.json"
            if not f.exists():
                return None
            self._cache[item_id] = json.loads(f.read_text(encoding="utf-8"))
        item = dict(self._cache[item_id])
        return self.apply_overrides(item) if apply_overrides else item

    def save_item(self, item: dict[str, Any]) -> None:
        write_json(self.items_dir / f"{item['id']}.json", item)
        self._cache.pop(item["id"], None)

    def remove_item(self, item_id: str) -> None:
        """Take an item out of the published store (file and in-memory cache)."""
        (self.items_dir / f"{item_id}.json").unlink(missing_ok=True)
        self._cache.pop(item_id, None)

    def save_record(self, rec: Record) -> dict[str, Any]:
        item = rec.to_json()
        self.save_item(item)
        return item

    def get_by_url(self, url: str) -> dict[str, Any] | None:
        return self.load_item(url_id(url))

    # -- rejected / review --------------------------------------------------
    def log_rejected(self, url: str, source_id: str, reason: str, detail: str = "") -> None:
        key = (url, reason)
        if self._rejected_logged is None:
            self._rejected_logged = set()
            if self.rejected_path.exists():
                for line in self.rejected_path.read_text(encoding="utf-8").splitlines():
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._rejected_logged.add((e.get("url", ""), e.get("reason", "")))
        if key in self._rejected_logged:
            return  # already logged: the rejected log records decisions, not sightings
        self._rejected_logged.add(key)
        if self.dry_run:
            return
        entry = {
            "url": url, "source": source_id, "reason": reason,
            "detail": detail, "seen": time.strftime("%Y-%m-%d"),
        }
        with self.rejected_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def known_urls(self, today: str | None = None) -> set[str]:
        """URL ids already decided on: published items, pending review records and
        logged rejections. Candidates matching these are skipped by cmd_run so
        runs are idempotent and decided items are never re-proposed.

        A rejection that is only a transient fetch failure (connect error, timeout,
        429, 5xx) is not a decision: such URLs are retried on later runs until
        `qualify.retry_failed_fetch_days` (pack.yml) have passed since the first
        failure. The first run lost 166 apps to a network blip this way."""
        known: set[str] = set()
        for folder in (self.items_dir, self.pack.private_dir / "review"):
            if not folder.exists():
                continue
            for f in folder.glob("*.json"):
                try:
                    rec = json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    known.add(f.stem)
                    continue
                known.add(url_id(rec.get("url") or f.stem))
        days = int((self.pack.pack.get("qualify") or {})
                   .get("retry_failed_fetch_days", DEFAULT_RETRY_DAYS))
        today_d = dt.date.fromisoformat(today) if today else dt.date.today()
        retry: set[str] = set()
        if self.rejected_path.exists():
            for line in self.rejected_path.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not e.get("url"):
                    continue
                uid = url_id(e["url"])
                if is_retryable_rejection(e.get("reason", "")) and _within(e.get("seen"), today_d, days):
                    retry.add(uid)
                else:
                    known.add(uid)
        # Any final answer (a 404, low confidence, a review record…) wins over a
        # pending retry for the same URL.
        self.retrying = len(retry - known)
        return known

    def add_sighting(self, item_id: str, provenance: list[dict]) -> bool:
        """A decided app (published or in review) was found again. Add any source
        it hasn't been credited to and refresh `quality.mentions`; nothing else
        changes and nothing is refetched. Returns True if the record changed."""
        for folder in (self.items_dir, self.pack.private_dir / "review"):
            path = folder / f"{item_id}.json"
            if not path.exists():
                continue
            rec = json.loads(path.read_text(encoding="utf-8"))
            have = {p.get("source") for p in rec.get("provenance", [])}
            new = [p for p in provenance if p.get("source") not in have]
            if not new:
                return False
            if self.dry_run:
                return True
            rec.setdefault("provenance", []).extend(new)
            rec.setdefault("quality", {})["mentions"] = len(have | {p["source"] for p in new})
            write_json(path, rec)
            self._cache.pop(item_id, None)
            return True
        return False

    def save_review(self, rec: Record, confidence: float) -> Path:
        review_dir = self.pack.private_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        # include_text keeps page text + raw source payload on the review record
        # so `reclassify` can re-score later without refetching the web
        item = rec.to_json(include_text=True)
        item["quality"]["confidence"] = confidence
        item["quality"]["needs"] = "review"
        # rec.id can be empty for fresh review candidates; fall back to the URL-derived id
        item_id = rec.id or url_id(rec.url)
        item["id"] = item_id
        path = review_dir / f"{item_id}.json"
        write_json(path, item)
        return path

    # -- run state ----------------------------------------------------------
    def run_state_path(self) -> Path:
        return self.pack.private_dir / "run_state.json"

    def load_run_state(self) -> dict[str, Any]:
        return load_json(self.run_state_path()) or {}

    def save_run_state(self, state: dict[str, Any]) -> None:
        write_json(self.run_state_path(), state)


def _within(seen: str | None, today: dt.date, days: int) -> bool:
    """True while the retry window opened by a failure on `seen` is still open.
    The rejected log keeps one line per (url, reason), so `seen` is the first
    failure and repeated failures cannot extend the window indefinitely."""
    try:
        return (today - dt.date.fromisoformat(seen or "")).days < days
    except ValueError:
        return False
