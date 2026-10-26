"""Stage 1 — Discover: fetch every active source, normalise, merge with known items."""

from __future__ import annotations

import logging
import time
from typing import Any

from engine import adapters
from engine.http import load_conditional_cache, save_conditional_cache
from engine.models import Candidate
from engine.util import url_id

log = logging.getLogger("engine")


# Raw keys that locate a sighting (the HN thread, the repo); qualify turns them
# into provenance links.
_SIGHTING_KEYS = ("hn_id", "repo_url", "is_repo", "listing_url")
_TIER_RANK = {"A": 0, "B": 1, "C": 2}


def _add_sighting(first: Candidate, again: Candidate, tier: str) -> None:
    """Record that another source (or another query of the same one) found the
    same app. Second sightings used to be dropped here, so corroboration and
    `mentions` never had anything to count. The best trust tier wins."""
    first.raw.setdefault("sightings", []).append({
        "source_id": again.source_id, "tier": tier,
        **{k: again.raw[k] for k in _SIGHTING_KEYS if again.raw.get(k)},
    })
    if _TIER_RANK.get(tier, 1) < _TIER_RANK.get(first.raw.get("source_tier", "B"), 1):
        first.raw["source_tier"] = tier


def run(ctx, sources: list[dict], only: str | None = None) -> list[Candidate]:
    """Fetch sources; returns merged, deduplicated candidates."""
    load_conditional_cache(ctx.pack.private_dir / "http_cache.json")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    all: dict[str, Candidate] = {}
    per_source: dict[str, dict[str, Any]] = {}

    for src in sources:
        sid = src.get("id", "?")
        if src.get("status", "active") == "paused":
            per_source[sid] = {"status": "paused", "found": 0, "new": 0, "checked": stamp}
            continue
        if only and sid != only:
            continue
        try:
            adapter = adapters.get_adapter(src)
            found = new = 0
            tier = src.get("trust", "B")
            for cand in adapter.fetch(src.get("url", ""), None):
                found += 1
                key = url_id(cand.url)
                if key in all:
                    _add_sighting(all[key], cand, tier)
                    continue
                cand.raw.setdefault("source_tier", tier)
                all[key] = cand
                new += 1
            per_source[sid] = {"status": "ok", "found": found, "new": new, "checked": stamp}
            log.info("source %s: found=%d new=%d", sid, found, new)
        except Exception as e:  # noqa: BLE001 — one failing source never fails the run
            log.warning("source %s failed: %s", sid, e)
            per_source[sid] = {"status": "error", "error": str(e)[:200],
                               "found": 0, "new": 0, "checked": stamp}

    save_conditional_cache(ctx.pack.private_dir / "http_cache.json")
    ctx.source_results = per_source
    return list(all.values())
