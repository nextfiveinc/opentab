"""Evaluation set (P0): measure a classifier config against hand labels before tuning.

Two files under data/eval/:
  cases.jsonl  frozen inputs: page text, page metadata and signals, and the source
               post, captured once by `engine eval-cases`, so every model is scored on
               identical inputs without refetching the web.
  labels.yml   the expected answers per case id, written and corrected by humans.

`engine eval` runs the real Classifier and gate over the cases (writing nothing to the
catalogue) and reports how often the verdict and each labelled field match. Pass
`--model` (repeatable) to compare decision models from the same labels: thresholds are
re-suggested per model, because a threshold belongs to the model it was tuned on.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

from engine.models import Record

log = logging.getLogger("engine")

HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"


def eval_dir(pack) -> Path:
    return pack.private_dir / "eval"


# ---- building cases -----------------------------------------------------------------

def _hn_post(client, thread_url: str) -> tuple[str, str]:
    from engine.http import get_json
    from engine.util import strip_text

    m = re.search(r"item\?id=(\d+)", thread_url or "")
    if not m:
        return "", ""
    data = get_json(HN_ITEM.format(id=m.group(1)), client) or {}
    return data.get("title") or "", strip_text(data.get("text") or "", limit=2000)


def build_cases(ctx, limit: int = 200, include: tuple[str, ...] = ("items", "review")) -> dict:
    """Snapshot inputs for published and queued records (one page fetch each)."""
    from engine.http import fetch
    from engine.stages.enrich import extract_page_meta, page_signals
    from engine.util import strip_text

    out_path = eval_dir(ctx.pack) / "cases.jsonl"
    existing = {c["id"] for c in load_cases(ctx.pack)}
    records = []
    for folder in include:
        base = ctx.pack.private_dir if folder == "review" else ctx.pack.data_dir
        for f in sorted((base / folder).glob("*.json")):
            rec = json.loads(f.read_text(encoding="utf-8"))
            if rec.get("id", f.stem) not in existing:
                records.append((folder, rec))
    added = failed = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with ctx.http_client() as client, out_path.open("a", encoding="utf-8") as out:
        for folder, rec in records[:max(0, limit - len(existing))]:
            result = fetch(rec["url"], client)
            if not result.ok:
                failed += 1
                continue
            thread = next((p.get("url") for p in rec.get("provenance", [])
                           if "news.ycombinator.com" in (p.get("url") or "")), "")
            src_title, src_text = _hn_post(client, thread)
            case = {
                "id": rec.get("id"), "url": rec["url"], "from": folder,
                "source_id": (rec.get("provenance") or [{}])[0].get("source", rec.get("source", "")),
                "tier": rec.get("tier", "B"), "provenance": rec.get("provenance", []),
                "source_title": src_title, "source_text": src_text,
                "page_meta": extract_page_meta(result.body or ""),
                "signals": page_signals(result.body or "", result.url),
                "text": strip_text(result.body or "", limit=4000),
            }
            out.write(json.dumps(case, ensure_ascii=False) + "\n")
            added += 1
    return {"cases": len(existing) + added, "added": added, "fetch_failed": failed,
            "file": str(out_path)}


def load_cases(pack) -> list[dict]:
    path = eval_dir(pack) / "cases.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_labels(pack) -> dict[str, dict]:
    path = eval_dir(pack) / "labels.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return (data or {}).get("labels", {}) or {}


def record_from_case(case: dict) -> Record:
    meta = case.get("page_meta") or {}
    return Record(
        id=case["id"], url=case["url"], source_id=case.get("source_id", ""),
        title=meta.get("title") or case.get("source_title", ""), text=case.get("text"),
        tier=case.get("tier", "B"), provenance=list(case.get("provenance") or []),
        raw={"page_meta": meta, "signals": case.get("signals") or {},
             "source_title": case.get("source_title", ""), "source_text": case.get("source_text", ""),
             "source_tier": case.get("tier", "B")},
        dates={"first_seen": "2026-01-01"},
    )


# ---- scoring --------------------------------------------------------------------------

# label key -> how to read the prediction from a classified record
FIELDS = {
    "category": lambda r: r.category,
    "is_game": lambda r: "game" in r.flags,
    "is_chat_wrapper": lambda r: "chat-wrapper" in r.flags,
    "needs_sign_in": lambda r: r.attrs.get("access") == "required",
}


def _score(rows: list[dict], labels: dict[str, dict]) -> dict:
    """Verdict confusion and per-field accuracy over labelled rows."""
    confusion = {"listable": {"publish": 0, "review": 0, "rejected": 0},
                 "not_listable": {"publish": 0, "review": 0, "rejected": 0}}
    fields = {k: [0, 0] for k in FIELDS}
    misses = []
    for row in rows:
        lab = labels[row["id"]]
        exp = "listable" if lab.get("listable") else "not_listable"
        confusion[exp][row["verdict"]] += 1
        kept = row["verdict"] != "rejected"
        if kept != bool(lab.get("listable")):
            misses.append({"id": row["id"], "url": row["url"], "expected": exp,
                           "got": row["verdict"], "confidence": row["confidence"],
                           "reasons": row["reasons"], "note": lab.get("note", "")})
        for k in FIELDS:
            if k in lab and (k != "category" or lab.get("listable")):
                fields[k][1] += 1
                fields[k][0] += int(row["fields"][k] == lab[k])
    n = len(rows)
    tp = confusion["listable"]["publish"] + confusion["listable"]["review"]
    fp = confusion["not_listable"]["publish"] + confusion["not_listable"]["review"]
    fn = confusion["listable"]["rejected"]
    tn = confusion["not_listable"]["rejected"]
    return {
        "labelled": n,
        "keep_vs_reject_accuracy": round((tp + tn) / n, 3) if n else None,
        "listable_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "rejects_correct": round(tn / (tn + fp), 3) if tn + fp else None,
        "wrongly_published": confusion["not_listable"]["publish"],
        "confusion": confusion,
        "field_accuracy": {k: (round(a / t, 3) if t else None) for k, (a, t) in fields.items()},
        "field_counts": {k: t for k, (_, t) in fields.items()},
        "misses": misses[:25],
    }


def _threshold_sweep(rows: list[dict], labels: dict[str, dict]) -> list[dict]:
    """Keep/reject quality if the review floor were each value (decide mode only)."""
    out = []
    for t in (0.2, 0.3, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8):
        tp = fp = fn = tn = 0
        for row in rows:
            keep = not row["reasons"] and row["confidence"] >= t
            good = bool(labels[row["id"]].get("listable"))
            tp += keep and good
            fp += keep and not good
            fn += (not keep) and good
            tn += (not keep) and not good
        out.append({"review_floor": t, "kept": tp + fp, "listable_recall": round(tp / (tp + fn), 3) if tp + fn else None,
                    "precision_of_kept": round(tp / (tp + fp), 3) if tp + fp else None})
    return out


def run_eval(ctx, models: list[str] | None = None, mode: str | None = None) -> dict:
    from engine.stages import gate
    from engine.stages.enrich import Classifier

    cases = load_cases(ctx.pack)
    labels = load_labels(ctx.pack)
    labelled = [c for c in cases if c["id"] in labels]
    if not labelled:
        return {"error": "no labelled cases: run `engine eval-cases`, then label data/eval/labels.yml"}

    if mode:
        ctx.pack.classifier["mode"] = mode
    from engine.models import Candidate
    from engine.stages.qualify import rules_match

    engine_log = logging.getLogger("engine")
    level = engine_log.level
    engine_log.setLevel(logging.WARNING)  # one gate line per case is noise here
    runs = []
    for model in (models or [None]):
        clf = Classifier(ctx, decide_model=model)
        rows = []
        for case in labelled:
            rec = record_from_case(case)
            # Same order as the pipeline: rules.yml first, AI only for survivors.
            rule = rules_match(ctx.pack, Candidate(url=case["url"], title=case.get("source_title") or rec.title,
                                                   text=case.get("source_text")))
            if rule:
                rec.status, rec.reasons = "rejected", [rule]
            else:
                clf.classify(rec)
                gate.run(ctx, [rec])  # ctx is dry-run: nothing is logged or written
            rows.append({"id": rec.id, "url": rec.url, "verdict": {"active": "publish"}.get(rec.status, rec.status),
                         "confidence": rec.quality.get("confidence", 0.0), "reasons": rec.reasons,
                         "fields": {k: f(rec) for k, f in FIELDS.items()}})
        clf.flush()
        result = {"mode": clf.stats["mode"], "model": model or (ctx.pack.decide.get("model") if clf.mode == "decide" else None),
                  **_score(rows, labels), "classifier": clf.stats}
        if clf.stats["mode"] == "decide":
            result["threshold_sweep"] = _threshold_sweep(rows, labels)
        runs.append(result)
    engine_log.setLevel(level)
    return {"cases": len(cases), "labelled": len(labelled), "runs": runs}


def summarise(report: dict) -> str:
    """Short human table for the terminal (the JSON report has everything)."""
    if "runs" not in report:
        return json.dumps(report, indent=2)
    lines = [f"{report['labelled']} labelled cases (of {report['cases']})", ""]
    lines.append(f"{'mode/model':42} {'keep/reject':>11} {'recall':>7} {'rejects ok':>10} {'wrong pub':>9}")
    for r in report["runs"]:
        name = f"{r['mode']}:{r['model']}" if r.get("model") else r["mode"]
        lines.append(f"{name[:42]:42} {r['keep_vs_reject_accuracy']!s:>11} {r['listable_recall']!s:>7} "
                     f"{r['rejects_correct']!s:>10} {r['wrongly_published']!s:>9}")
        lines.append("   fields: " + ", ".join(f"{k}={v}" for k, v in r["field_accuracy"].items()))
    return "\n".join(lines)
