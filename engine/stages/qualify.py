"""Stage 2 — Qualify: deterministic rules, one page fetch, safety checks."""

from __future__ import annotations

import logging
import re

from engine.http import fetch
from engine.models import Candidate, Record
from engine.util import looks_like_parked, strip_text, url_id

log = logging.getLogger("engine")

PARKED_PATTERNS = [
    r"\bbuy this domain\b", r"\bthis domain is for sale\b", r"\bdomain (is )?parked\b",
    r"^login", r"^sign in", r"^create an account$",
]


def rules_match(pack, cand: Candidate) -> str | None:
    """Returns an exclusion reason or None. Include/exclude regexes from rules.yml."""
    url = cand.url.lower()
    title = cand.title.lower()
    blob = f"{url} {title} {(cand.text or '').lower()[:500]}"

    for rule in pack.rules.get("exclude", []):
        if re.search(rule.get("match", ""), blob, re.IGNORECASE):
            return f"exclude:{rule.get('reason', rule.get('match', ''))}"

    include = pack.rules.get("include", [])
    if include and not any(re.search(r.get("match", ""), blob, re.IGNORECASE) for r in include):
        return "exclude:no-include-rule-matched"
    return None


def _provenance_url(raw: dict, app_url: str) -> str:
    """Where the candidate was found — the HN thread or repo, not the app URL."""
    if raw.get("hn_id"):
        return f"https://news.ycombinator.com/item?id={raw['hn_id']}"
    if raw.get("repo_url") and raw.get("is_repo"):
        return raw["repo_url"]
    if raw.get("listing_url"):
        return raw["listing_url"]
    return app_url


def provenance_entries(cand: Candidate, today: str) -> list[dict]:
    """One provenance entry per source that found the candidate (first sighting
    plus any later ones discover merged), deduped by source."""
    out: list[dict] = []
    seen: set[str] = set()
    for source_id, raw in [(cand.source_id, cand.raw),
                           *((s.get("source_id", ""), s) for s in cand.raw.get("sightings", []))]:
        if source_id in seen:
            continue
        seen.add(source_id)
        out.append({"source": source_id, "url": _provenance_url(raw, cand.url),
                    "item_url": cand.url, "seen": today})
    return out


def run(ctx, candidates: list[Candidate]) -> list:
    """Cheap checks first, then one fetch to confirm liveness. Returns surviving records."""
    survivors: list = []
    refused = 0
    for cand in candidates:
        reason = rules_match(ctx.pack, cand)
        if reason:
            ctx.store.log_rejected(cand.url, cand.source_id, reason)
            refused += 1
            continue

        rec = Record(
            id=url_id(cand.url), url=cand.url, source_id=cand.source_id, title=cand.title,
            text=cand.text,
            # The source's own words (an HN post, a listing) are kept apart from the page:
            # they are the evidence for creator claims. They used to be overwritten by
            # the page text, and the classifier's "source description" was always empty.
            raw={**cand.raw, "source_title": cand.title, "source_text": (cand.text or "")[:2000]},
            tier=cand.raw.get("source_tier", "B"),
            provenance=provenance_entries(cand, ctx.today),
            dates={"first_seen": ctx.today},
        )

        # One page fetch: does it load, is it parked or login-walled? The same
        # response also yields the page metadata enrich needs — no second fetch.
        with ctx.http_client() as client:
            result = fetch(cand.url, client)
        if not result.ok:
            ctx.store.log_rejected(cand.url, cand.source_id, f"fetch-failed:{result.status or result.error}")
            refused += 1
            continue
        text = strip_text(result.body or "", limit=4000)
        parked = looks_like_parked(rec.title, text, cand.url)
        if parked:
            ctx.store.log_rejected(cand.url, cand.source_id, parked)
            refused += 1
            continue

        # Safety lookup placeholder: hook a reputation service here when a key exists.
        from engine.stages.enrich import extract_page_meta, page_signals
        rec.raw["page_meta"] = extract_page_meta(result.body or "")
        rec.raw["signals"] = page_signals(result.body or "", result.url)
        rec.text = text
        rec.liveness = {"last_check": ctx.today, "status": "ok", "http": result.status}
        survivors.append(rec)
    ctx.qualify_rejected = refused
    return survivors
