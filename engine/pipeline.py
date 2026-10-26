"""Pipeline orchestration: run, build, lifecycle commands share a Context."""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from engine.config import Pack, load_json, load_pack, write_json
from engine.store import Store

log = logging.getLogger("engine")


@dataclass
class Context:
    pack: Pack
    store: Store
    today: str
    yesterday: str
    source_results: dict[str, Any] = field(default_factory=dict)
    qualify_rejected: int = 0
    dry_run: bool = False
    classifier_stats: dict[str, Any] = field(default_factory=dict)
    _client: httpx.Client | None = field(default=None, repr=False)

    @contextmanager
    def http_client(self) -> Iterator[httpx.Client]:
        """One client for the whole command, so page fetches to the same host
        reuse connections (a new client per candidate used to open a new TLS
        connection for every page). Closed when the process exits."""
        if self._client is None:
            import atexit

            self._client = httpx.Client()
            atexit.register(self._client.close)
        yield self._client

    def today_str_diff(self, earlier: str) -> int:
        try:
            d = dt.date.fromisoformat(earlier)
        except ValueError:
            return 9999
        return (dt.date.fromisoformat(self.today) - d).days


def _context(pack_path: str, dry_run: bool = False, private: bool = True) -> Context:
    """`private`: the command reads or writes working data (review queue, caches, run
    report). Without the private folder it stops here, instead of quietly starting an
    empty queue in the wrong place. Build, validate and thumbs need only public data."""
    pack = load_pack(pack_path)
    if private and pack.private_missing:
        raise SystemExit(
            f"working-data folder not found: {pack.private_dir}\n"
            "Clone the private data repository there (README_LOCAL.md → Private working data).")
    today = dt.datetime.now(dt.UTC).date()
    return Context(
        pack=pack,
        store=Store(pack, dry_run=dry_run),
        today=today.isoformat(),
        yesterday=(today - dt.timedelta(days=1)).isoformat(),
        dry_run=dry_run,
    )


def cmd_run(pack_path: str, only: str | None = None, dry_run: bool = False,
            max_calls: int | None = None) -> dict:
    """Discover -> qualify -> enrich -> gate -> (persist). Then build separately.
    `max_calls` raises the classifier's per-run safety fuse for a one-off bulk import;
    past the fuse, candidates fall back to rules-only classification."""
    from engine.stages import discover, enrich, gate, qualify
    from engine.stages.qualify import provenance_entries
    from engine.util import url_id

    ctx = _context(pack_path, dry_run)
    if max_calls:
        ctx.pack.classifier["max_calls_per_run"] = max_calls
    sources = ctx.pack.sources
    candidates = discover.run(ctx, sources, only=only)
    log.info("discover: %d candidates", len(candidates))

    # Never re-propose a URL we have already decided on (published item, pending
    # review record, or logged rejection). Keeps runs idempotent and stops the
    # same app being refetched and re-billed every day.
    known = ctx.store.known_urls(ctx.today)
    skipped_known = resighted = 0
    fresh = []
    for cand in candidates:
        if url_id(cand.url) in known:
            skipped_known += 1
            # A known app found by a new source is corroboration, not noise.
            if ctx.store.add_sighting(url_id(cand.url), provenance_entries(cand, ctx.today)):
                resighted += 1
            continue
        fresh.append(cand)
    if skipped_known:
        log.info("skip known: %d of %d already decided", skipped_known, len(candidates))
    if ctx.store.retrying:
        log.info("retry: %d URLs whose last fetch failed transiently are eligible again",
                 ctx.store.retrying)
    candidates = fresh

    # Tier C (weak signals) only becomes a candidate when a second source has seen
    # the same app. Not logged as a rejection, so a later corroboration still counts.
    uncorroborated = [c for c in candidates
                      if c.raw.get("source_tier") == "C"
                      and len({c.source_id, *(s.get("source_id") for s in c.raw.get("sightings", []))}) < 2]
    if uncorroborated:
        log.info("tier C: %d candidates wait for a second source", len(uncorroborated))
        candidates = [c for c in candidates if c not in uncorroborated]

    records = qualify.run(ctx, candidates)
    log.info("qualify: %d records (%d refused)", len(records), ctx.qualify_rejected)

    records = enrich.run(ctx, records)
    log.info("enrich: %d records", len(records))

    verdict = gate.run(ctx, records)

    if not dry_run:
        for rec in verdict["publish"]:
            # Raw stored item (no overrides), so human edits in overrides.yml are
            # neither clobbered by this run nor baked into the item file.
            existing = ctx.store.load_item(rec.id, apply_overrides=False)
            if existing:
                # Idempotence: don't rewrite unchanged items
                merged = _merge_existing(existing, rec,
                                         protected=set(ctx.store.overrides.get(rec.id, {})))
                ctx.store.save_item(merged)
            else:
                ctx.store.save_item(_published(rec))
        for rec in verdict["review"]:
            # Review candidates live in data/review/, not the canonical item store,
            # until a human approves them (plan: mid-confidence awaiting a decision).
            ctx.store.save_review(rec, rec.quality.get("confidence", 0.0))

    report = {
        "pack": ctx.pack.id,
        "date": ctx.today,
        "dry_run": dry_run,
        "candidates": len(candidates),
        "skipped_known": skipped_known,
        "new_sources_on_known": resighted,
        "fetch_retry_eligible": ctx.store.retrying,
        "qualified": len(records),
        "qualify_rejected": ctx.qualify_rejected,
        "tier_c_waiting": len(uncorroborated),
        "classifier": ctx.classifier_stats,
        "publish": len(verdict["publish"]),
        "review": len(verdict["review"]),
        "rejected": len(verdict["rejected"]),
        "sources": _merged_source_results(ctx),
    }
    if not dry_run:
        write_json(ctx.pack.private_dir / "last_report.json", report)
    return report


def _merged_source_results(ctx) -> dict[str, Any]:
    """This run's per-source results on top of the previous report's. A run with
    `--only` checks one source; replacing the whole map would blank every other
    source's last-checked date and health."""
    previous = (load_json(ctx.pack.private_dir / "last_report.json") or {})
    merged = {}
    for sid, result in (previous.get("sources") or {}).items():
        if isinstance(result, dict):
            merged[sid] = {**result, "checked": result.get("checked") or previous.get("date", "")}
    merged.update(ctx.source_results)
    return merged


def _published(rec) -> dict[str, Any]:
    """A record as stored on publish: no page text, plus the fingerprint lifecycle
    compares against to catch a domain that changed hands."""
    from engine.stages.lifecycle import fingerprint_from_raw

    item = rec.to_json()
    fp = fingerprint_from_raw(rec.raw)
    if fp:
        item["fingerprint"] = fp
    return item


def _merge_existing(existing: dict[str, Any], rec, protected: set[str] | None = None) -> dict[str, Any]:
    """Merge a fresh classification into a stored item. Human overrides and a
    human 'hidden' flag always win over automation."""
    protected = protected or set()
    merged = dict(existing)
    for key in ("summary", "why", "category", "subcategory", "tags", "attrs", "ai_claim",
                "evidence", "quality", "flags"):
        if key in protected:
            continue
        incoming = getattr(rec, key, None) if not isinstance(rec, dict) else rec.get(key)
        if incoming is None or incoming == "" or incoming == []:
            continue
        if key == "flags" and "hidden" in existing.get("flags", []):
            continue  # human-hidden stays hidden
        merged[key] = incoming
    # provenance: append new sources, dedupe
    prov = {p.get("url"): p for p in existing.get("provenance", [])}
    for p in rec.provenance:
        prov.setdefault(p.get("url"), p)
    merged["provenance"] = list(prov.values())
    return merged


def cmd_build(pack_path: str, out_dir: str = "dist", dry_run: bool = False) -> dict:
    from engine.stages import build

    ctx = _context(pack_path, dry_run, private=False)
    return build.run(ctx, Path(out_dir), dry_run=dry_run)


def cmd_lifecycle(pack_path: str, everything: bool = False) -> dict:
    from engine.stages import lifecycle

    ctx = _context(pack_path)
    return lifecycle.run(ctx, everything=everything)


def cmd_reclassify(pack_path: str, dry_run: bool = False, keep: list[str] | None = None) -> dict:
    """Re-run classification over existing review items, through the same Classifier
    and gate as `run` (decision model + text pass in `decide` mode).

    Each review record keeps its identity, dates and provenance. Tier-A items at
    or above the publish threshold are promoted to data/items/, items the gate rejects
    are logged with a reason and removed, the rest stay in review, rewritten in place.
    Records saved before page text and page signals were stored get one page refetch.
    `dry_run` reports the verdicts without writing anything. Ids in `keep` stay in
    review whatever the gate decides (a human wants to look first): their would-be
    verdict is recorded in quality.held_for_human and nothing is logged as rejected.
    """
    from engine.models import Record
    from engine.stages import gate
    from engine.stages.enrich import Classifier, extract_page_meta, page_signals
    from engine.util import strip_text, url_id

    ctx = _context(pack_path, dry_run)
    review_dir = ctx.pack.private_dir / "review"
    review_files = sorted(review_dir.glob("*.json"))
    if not review_files:
        return {"reclassified": 0, "promoted": 0, "note": "review queue is empty"}

    clf = Classifier(ctx)
    if clf.mode == "rules":
        return {"reclassified": 0, "promoted": 0,
                "error": "OPENROUTER_API_KEY is not set; nothing to reclassify"}

    from engine.http import fetch

    pairs: list[tuple[Path, Record]] = []
    refetched = 0
    for path in review_files:
        stored = json.loads(path.read_text(encoding="utf-8"))
        rec = Record.from_json(stored)
        rec.id = rec.id or url_id(rec.url)
        rec.tier = stored.get("tier", rec.tier)
        if rec.text is None or not rec.raw.get("signals"):
            with ctx.http_client() as client:
                result = fetch(rec.url, client)
            if result.ok:
                rec.text = strip_text(result.body or "", limit=4000)
                rec.raw["page_meta"] = extract_page_meta(result.body or "")
                rec.raw["signals"] = page_signals(result.body or "", result.url)
                refetched += 1
        clf.classify(rec)
        pairs.append((path, rec))
    clf.flush()

    keep_ids = set(keep or [])
    gate.run(ctx, [rec for _, rec in pairs if rec.id not in keep_ids])
    held = [rec for _, rec in pairs if rec.id in keep_ids]
    if held:
        # Same gate, but nothing logged: the verdict is advice for the human.
        logging_was, ctx.store.dry_run = ctx.store.dry_run, True
        gate.run(ctx, held)
        ctx.store.dry_run = logging_was
        for rec in held:
            rec.quality["held_for_human"] = {"would_be": rec.status, "reasons": list(rec.reasons),
                                             "date": ctx.today}
            rec.status = "review"

    if dry_run:
        verdicts = {"active": 0, "review": 0, "rejected": 0}
        for _, rec in pairs:
            verdicts[rec.status] = verdicts.get(rec.status, 0) + 1
        rows = sorted(({"id": rec.id, "verdict": rec.status, "confidence": rec.quality.get("confidence", 0.0),
                        "category": rec.category, "title": rec.title[:70], "url": rec.url,
                        "reasons": rec.reasons or [], "flags": rec.flags,
                        "disagreements": rec.quality.get("disagreements", [])} for _, rec in pairs),
                      key=lambda r: (r["verdict"], -r["confidence"]))
        return {"dry_run": True, "reclassified": len(pairs), "would": verdicts,
                "page_refetched": refetched, "classifier": clf.stats, "items": rows}

    promoted = kept = rejected = 0
    for path, rec in pairs:
        if rec.status == "active":
            ctx.store.save_item(_published(rec))  # no page text in published items
            path.unlink()
            promoted += 1
        elif rec.status == "review":
            rec.quality["needs"] = "review"
            write_json(path, rec.to_json(include_text=True))
            kept += 1
        else:  # rejected — the gate already logged the reason
            path.unlink()
            rejected += 1

    report = {
        "reclassified": len(pairs), "promoted": promoted,
        "kept_in_review": kept, "rejected": rejected,
        "page_refetched": refetched,
        "held_for_human": sorted(r.id for r in held),
        "missing_keep_ids": sorted(keep_ids - {rec.id for _, rec in pairs}),
        "classifier": clf.stats,
    }
    log.info("reclassify: %d reclassified, %d promoted, %d kept, %d rejected",
             len(pairs), promoted, kept, rejected)
    return report


def cmd_review(pack_path: str) -> dict:
    """List the review queue, most confident first, with what a decision needs."""
    ctx = _context(pack_path)
    rows = []
    for f in sorted((ctx.pack.private_dir / "review").glob("*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        rows.append({
            "id": f.stem,
            "confidence": round(float((r.get("quality") or {}).get("confidence", 0)), 2),
            "category": r.get("category", ""),
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "sources": sorted({p.get("source", "") for p in r.get("provenance", [])}),
        })
    rows.sort(key=lambda r: (-r["confidence"], r["title"]))
    return {"waiting": len(rows), "items": rows}


def cmd_approve(pack_path: str, item_id: str) -> dict:
    """Promote a review item to the published store (the 'merge approves' step of the
    daily review PR). Sets status to active and removes the review file."""
    ctx = _context(pack_path)
    review_path = ctx.pack.private_dir / "review" / f"{item_id}.json"
    if not review_path.exists():
        return {"approved": False, "reason": f"no review record for {item_id}"}
    item = json.loads(review_path.read_text(encoding="utf-8"))
    item["status"] = "active"
    quality = item.setdefault("quality", {})
    quality.pop("needs", None)
    # Approving a drifted item accepts what the page is now.
    drift = quality.pop("drift", None)
    if drift and drift.get("now"):
        item["fingerprint"] = drift["now"]
        item["flags"] = [f for f in item.get("flags", []) if f != "content-drift"]
    if not item.get("fingerprint"):
        from engine.stages.lifecycle import fingerprint_from_raw
        fp = fingerprint_from_raw(item.get("raw") or {})
        if fp:
            item["fingerprint"] = fp
    if not item.get("image"):
        from engine.stages.enrich import preview_image
        image = preview_image((item.get("raw") or {}).get("page_meta") or {},
                              item.get("url", ""), item.get("title", ""))
        if image:
            item["image"] = image
    # Published items stay lean (the plan's item record has no page text); the
    # classification input is only needed while the record is under review.
    item.pop("text", None)
    item.pop("raw", None)
    ctx.store.save_item(item)
    review_path.unlink()
    return {"approved": True, "id": item_id, "url": item.get("url"), "title": item.get("title")}


def cmd_reject(pack_path: str, item_id: str, reason: str = "human-reject") -> dict:
    """Drop a review item (the 'closing rejects' step of the daily review PR).
    Logs it to rejected.jsonl so a re-submitted tip is recognised."""
    ctx = _context(pack_path)
    review_path = ctx.pack.private_dir / "review" / f"{item_id}.json"
    if not review_path.exists():
        return {"rejected": False, "reason": f"no review record for {item_id}"}
    item = json.loads(review_path.read_text(encoding="utf-8"))
    ctx.store.log_rejected(item.get("url", ""), "review", reason)
    review_path.unlink()
    return {"rejected": True, "id": item_id, "url": item.get("url")}


def cmd_validate(pack_path: str) -> dict:
    """Validate pack config and stored data (items and the review queue) against schemas."""
    import jsonschema

    from engine.llm.schemas import build_response_schema

    ctx = _context(pack_path, private=False)   # checks the review queue only if it is here
    schema = build_response_schema(ctx.pack)
    errors = []

    def check(where: str, iid: str, it: dict) -> None:
        payload = {
            "category": it.get("category"),
            "subcategory": it.get("subcategory") or None,
            "tags": it.get("tags", []),
            "summary": it.get("summary", ""),
            "why": it.get("why", ""),
            "confidence": it.get("quality", {}).get("confidence", 0.5),
            "is_game": "game" in it.get("flags", []),
            "is_chat_wrapper": "chat-wrapper" in it.get("flags", []),
            "ai_claim": it.get("ai_claim", "none"),
            **(it.get("attrs") or {}),
        }
        try:
            jsonschema.validate(payload, schema)
        except jsonschema.ValidationError as e:
            errors.append({"id": iid, "where": where, "error": e.message})

    from engine.util import url_id

    items = ctx.store.load_items()
    for iid, it in items.items():
        check("items", iid, it)
        if it.get("url") and url_id(it["url"]) != iid:
            errors.append({"id": iid, "where": "items",
                           "error": f"id does not match url_id(url)={url_id(it['url'])}"})

    review_dir = ctx.pack.private_dir / "review"
    review = []
    if review_dir.exists():
        for f in sorted(review_dir.glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                errors.append({"id": f.stem, "where": "review", "error": "invalid JSON"})
                continue
            review.append(rec)
            check("review", rec.get("id", f.stem), rec)
            if rec.get("url") and url_id(rec["url"]) != f.stem:
                errors.append({"id": f.stem, "where": "review",
                               "error": f"id does not match url_id(url)={url_id(rec['url'])}"})

    errors.extend({"id": "decide.yml", "where": "pack", "error": e} for e in _check_decide(ctx.pack))
    return {"items": len(items), "review": len(review), "errors": errors}


def _check_decide(pack) -> list[str]:
    """decide.yml must be internally consistent: every map, reject rule and confidence
    term names a real question, and mapped values fit schema.yml."""
    cfg = pack.decide
    if pack.classifier.get("mode") != "decide" and not cfg:
        return []
    from engine.stages.decide import questions

    errs = []
    if not cfg.get("model"):
        errs.append("no `model`")
    if str(cfg.get("model", "")).startswith("~"):
        errs.append("`model` is an alias; pin a versioned id (thresholds belong to a build)")
    qs = questions(pack)
    for qid, q in qs.items():
        if q.get("type") not in ("noul", "choice", "score"):
            errs.append(f"question {qid}: type must be noul, choice or score")
        if q.get("type") in ("choice", "score") and not q.get("criteria"):
            errs.append(f"question {qid}: {q.get('type')} needs criteria")
        if not q.get("instructions"):
            errs.append(f"question {qid}: no instructions")
    schema = {f["id"]: f for f in pack.schema.get("fields", [])}
    for qid, m in (cfg.get("maps") or {}).items():
        if qid not in qs:
            errs.append(f"maps.{qid}: no such question")
            continue
        if "attr" in m and m["attr"] not in schema:
            errs.append(f"maps.{qid}: attr {m['attr']!r} is not in schema.yml")
        allowed = set(schema.get(m.get("attr"), {}).get("values") or [])
        produced = set((m.get("values") or {}).values()) | {v for k, v in m.items() if k in ("true", "false")}
        if allowed and produced - allowed:
            errs.append(f"maps.{qid}: values {sorted(produced - allowed)} not allowed by schema.yml")
    for section in ("reject",):
        for qid in (cfg.get(section) or {}):
            if qid not in qs:
                errs.append(f"{section}.{qid}: no such question")
    for qid in [*(cfg.get("confidence") or {}).get("true", []), *(cfg.get("confidence") or {}).get("false", [])]:
        if qid not in qs:
            errs.append(f"confidence: {qid}: no such question")
    return errs
