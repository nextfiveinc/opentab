"""Stage 4 — Gate: decide publish / review / reject from confidence, tier, corroboration."""

from __future__ import annotations

import logging

from engine.models import Record

log = logging.getLogger("engine")


def run(ctx, records: list[Record]) -> dict[str, list[Record]]:
    """Splits records into publish / review / rejected. Mutates rec.status and quality."""
    gate = ctx.pack.gate
    base_auto = float(gate.get("auto_publish", 0.85))
    base_review = float(gate.get("review", 0.60))
    review_mode = gate.get("review_mode", "queue")

    out = {"publish": [], "review": [], "rejected": []}

    # Corroboration: an app found by two or more independent sources gets its
    # confidence raised one band. Discover merges duplicate sightings into one
    # record, so count the sources on its provenance (grouping records by URL,
    # as this used to, never found a group).
    for rec in records:
        sources = {p.get("source") for p in rec.provenance if p.get("source")}
        rec.quality["mentions"] = max(1, len(sources))
        if len(sources) >= 2:
            rec.quality["confidence"] = min(1.0, rec.quality.get("confidence", 0.5) + 0.1)

    from engine.stages.decide import gate_thresholds

    decide_gate = gate_thresholds(ctx.pack)
    for rec in records:
        conf = rec.quality.get("confidence", 0.0)
        tier = rec.raw.get("source_tier", rec.tier)
        # Decision-model verdicts carry their own reject reasons and thresholds (tuned
        # for that model in decide.yml); other modes use pack.yml's gate.
        decided = rec.quality.get("classify_mode") == "decide"
        auto_at, review_at = ((decide_gate["auto_publish"], decide_gate["review"]) if decided
                              else (base_auto, base_review))
        reasons = list(rec.reasons) if decided else []

        # Hard exclusions win over everything
        if "chat-wrapper" in rec.flags and not decided:
            reasons.append("chat-wrapper")
        if conf < review_at:
            reasons.append(f"low-confidence:{conf:.2f}")

        if reasons:
            rec.reasons = reasons  # keep them on the record (reports, held-for-human notes)
            rec.status = "rejected"
            for r in reasons:
                ctx.store.log_rejected(rec.url, rec.source_id, r)
            out["rejected"].append(rec)
        elif conf >= auto_at and tier == "A":
            rec.status = "active"
            out["publish"].append(rec)
        elif review_mode == "auto":
            rec.status = "active"
            rec.flags.append("unreviewed")
            out["publish"].append(rec)
        elif review_mode == "off":
            rec.status = "rejected"
            ctx.store.log_rejected(rec.url, rec.source_id, "review-mode-off")
            out["rejected"].append(rec)
        else:  # queue
            rec.status = "review"
            out["review"].append(rec)

    log.info("gate: publish=%d review=%d rejected=%d",
             len(out["publish"]), len(out["review"]), len(out["rejected"]))
    return out
