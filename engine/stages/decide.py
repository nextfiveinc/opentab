"""Pass 1 of classification: turn a decision model's probabilities into a verdict.

Everything model- or question-specific comes from the pack's decide.yml (questions,
how answers map onto record fields, reject rules, the confidence formula and its gate).
This module applies those generically: it never names a model or a question id, so any
decision model can be swapped in by changing `model` in decide.yml.

Decision models read literally and are moved by text that argues for itself, so code
owns everything checkable: deterministic page signals are sent as plain facts, an
AI-built claim needs evidence outside the app's own page, and model answers that
contradict a deterministic check are logged and kept away from auto-publish.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from engine.models import Record

log = logging.getLogger("engine")

# Words that, in the *source* (the creator's post or listing), back an AI-built claim.
AI_TOOL_RE = re.compile(
    r"\b(claude|chatgpt|gpt-?\d|openai|gemini|copilot|cursor|windsurf|lovable|bolt\.new|"
    r"v0\.dev|v0|replit|codex|llms?|ai[- ](?:built|generated|assisted|coded)|vibe[- ]?cod\w*|"
    r"built with ai|made with ai)\b", re.IGNORECASE)


# ---- config -------------------------------------------------------------------------

def questions(pack) -> dict[str, dict]:
    """decide.yml questions, with `from: taxonomy` expanded into choice criteria."""
    out = {}
    for qid, q in (pack.decide.get("questions") or {}).items():
        q = dict(q)
        if q.pop("from", None) == "taxonomy":
            q["criteria"] = {
                cid: (f"{c.get('label', cid)}: " + ", ".join(
                    [s.get("label", s.get("id")) if isinstance(s, dict) else s
                     for s in c.get("subcategories", [])] + list(c.get("keywords", []))[:6]))
                .rstrip(": ")
                for cid, c in pack.categories.items()
            }
        out[qid] = q
    return out


def gate_thresholds(pack) -> dict[str, float]:
    g = pack.decide.get("gate") or {}
    base = pack.gate
    return {"auto_publish": float(g.get("auto_publish", base.get("auto_publish", 0.85))),
            "review": float(g.get("review", base.get("review", 0.60)))}


# ---- state -------------------------------------------------------------------------

def state_for(pack, rec: Record) -> dict[str, Any]:
    """What the model reads. Named fields, page text trimmed (accuracy drops with
    irrelevant bulk), deterministic signals stated as facts."""
    raw = rec.raw or {}
    meta = raw.get("page_meta") or {}
    limit = int(pack.decide.get("max_page_chars", 2500))
    state: dict[str, Any] = {
        "url": rec.url,
        "page_title": meta.get("title") or rec.title,
        "page_description": meta.get("description", ""),
        "found_on": rec.source_id,
        "source_post_title": raw.get("source_title", ""),
        "source_post_text": (raw.get("source_text") or "")[:800],
        "page_text": (rec.text or "")[:limit],
    }
    signals = raw.get("signals") or {}
    if signals:
        state["checked_by_code"] = {
            "page_has_password_field": bool(signals.get("login_form")),
            "url_redirected_to_sign_in": bool(signals.get("login_redirect")),
            "final_host": signals.get("final_host", ""),
        }
    return {k: v for k, v in state.items() if v not in ("", None, {})}


# ---- interpretation -----------------------------------------------------------------

def _prob(answer: Any, want: Any = True) -> float:
    """P(answer == want): noul probability, or a choice option's probability."""
    if isinstance(answer, float):
        return answer if want is True else 1.0 - answer
    if isinstance(answer, dict) and "choice" in answer:
        return float((answer.get("probabilities") or {}).get(want, 1.0 if answer["choice"] == want else 0.0))
    return 0.0


def compact(answers: dict[str, Any]) -> dict[str, Any]:
    """Audit copy stored on the record: probabilities, chosen options, levels."""
    out: dict[str, Any] = {}
    for qid, a in answers.items():
        if isinstance(a, float):
            out[qid] = round(a, 3)
        elif "choice" in a:
            out[qid] = {"choice": a["choice"],
                        "p": round(float((a.get("probabilities") or {}).get(a["choice"], 1.0)), 3)}
        else:
            out[qid] = {"level": a["level"]}
    return out


def apply(pack, rec: Record, answers: dict[str, Any], model: str) -> None:
    """Map answers onto the record, apply reject rules and the confidence formula."""
    cfg = pack.decide
    for qid, m in (cfg.get("maps") or {}).items():
        if qid not in answers:
            continue
        a = answers[qid]
        is_true = isinstance(a, float) and a >= float(m.get("at", 0.5))
        value = a["choice"] if isinstance(a, dict) and "choice" in a else is_true
        if isinstance(value, str) and m.get("values"):
            value = m["values"].get(value, value)
        elif isinstance(value, bool) and "true" in m:
            value = m["true"] if value else m.get("false")
        if "field" in m:
            setattr(rec, m["field"], value)
        elif "attr" in m:
            rec.attrs[m["attr"]] = value
        elif "flag" in m:
            rec.flags = [f for f in rec.flags if f != m["flag"]] + ([m["flag"]] if value else [])

    reasons = []
    for qid, rule in (cfg.get("reject") or {}).items():
        a = answers.get(qid)
        if a is None:
            continue
        p = _prob(a) if isinstance(a, float) else None
        if p is not None and "below" in rule and p < float(rule["below"]):
            reasons.append(f"decide:{qid}<{rule['below']}")
        if p is not None and "above" in rule and p > float(rule["above"]):
            reasons.append(f"decide:{qid}>{rule['above']}")
        if isinstance(a, dict) and "level" in a and "level_below" in rule and a["level"] < int(rule["level_below"]):
            reasons.append(f"decide:{qid}-level<{rule['level_below']}")
    rec.reasons = reasons

    conf = 1.0
    formula = cfg.get("confidence") or {}
    for qid in formula.get("true", []):
        conf *= _prob(answers.get(qid, 0.0), True)
    for qid in formula.get("false", []):
        conf *= _prob(answers.get(qid, 0.0), False) if isinstance(answers.get(qid), float) else 1.0
    rec.quality["confidence"] = round(conf, 4)
    rec.quality["classify_mode"] = "decide"
    rec.quality["decisions"] = compact(answers)
    rec.quality["decide_model"] = model  # the exact build: thresholds belong to it


def require_claim_evidence(rec: Record) -> None:
    """An AI-built claim needs support outside the app's own page: the creator's post
    or listing (source title/text). A page saying 'built with Claude' about itself is
    not enough, so such claims are downgraded to 'inferred'."""
    if rec.ai_claim not in ("self-declared", "platform-listed"):
        return
    raw = rec.raw or {}
    source_blob = f"{raw.get('source_title', '')} {raw.get('source_text', '')}"
    backed = rec.ai_claim == "self-declared" and AI_TOOL_RE.search(source_blob)
    if rec.ai_claim == "platform-listed":
        backed = bool(raw.get("platform_listing"))
    if backed:
        thread = next((p.get("url") for p in rec.provenance if p.get("url") and p.get("url") != rec.url), None)
        if thread and thread not in {e.get("url") for e in rec.evidence}:
            rec.evidence.append({"kind": "creator-statement" if rec.ai_claim == "self-declared"
                                 else "platform-listing", "url": thread})
    else:
        rec.quality.setdefault("downgraded", []).append(f"ai_claim:{rec.ai_claim}->inferred")
        rec.ai_claim = "inferred"


def cross_check(ctx, rec: Record) -> list[str]:
    """Deterministic checks the model's answers must not contradict. A disagreement is
    logged (data/disagreements.jsonl) and caps confidence below auto-publish, so a
    human sees the item: e.g. 'no sign-in needed' on a page with a password field."""
    sig = (rec.raw or {}).get("signals") or {}
    found = []
    if (sig.get("login_form") or sig.get("login_redirect")) and rec.attrs.get("access") == "none":
        found.append("access:none-but-login-detected")
    if sig.get("parked"):
        found.append("parked-page-signals")
    if found:
        cap = gate_thresholds(ctx.pack)["auto_publish"] - 0.01
        rec.quality["confidence"] = min(rec.quality.get("confidence", 0.0), cap)
        rec.quality["disagreements"] = found
        if not ctx.dry_run:
            path = ctx.pack.private_dir / "disagreements.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": rec.id, "url": rec.url, "checks": found,
                                    "decisions": rec.quality.get("decisions", {}),
                                    "model": rec.quality.get("decide_model", ""),
                                    "seen": time.strftime("%Y-%m-%d")}, ensure_ascii=False) + "\n")
    return found
