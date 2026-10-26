"""Markdown summary of a scheduled run, for the GitHub Actions job page and the data
commit message: what the run found, what lifecycle saw, and what waits for review."""

from __future__ import annotations

import json
from pathlib import Path


def _load(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def summarise(pack, run: str | None = None, lifecycle: str | None = None,
              thumbs: str | None = None) -> str:
    r, life, th = _load(run), _load(lifecycle), _load(thumbs)
    clf = r.get("classifier") or {}
    waiting = len(list((pack.private_dir / "review").glob("*.json")))
    published = len(list((pack.data_dir / "items").glob("*.json")))
    lines = [f"### {pack.pack.get('name', pack.id)}: update {r.get('date', '')}".rstrip(), ""]
    if r:
        lines += [
            (f"- **Run:** {r.get('candidates', 0)} new candidates "
             f"({r.get('skipped_known', 0)} already decided; "
             f"{r.get('fetch_retry_eligible', 0)} earlier fetch failures eligible again) → "
             f"{r.get('publish', 0)} published, {r.get('review', 0)} to review, {r.get('rejected', 0)} rejected "
             f"({r.get('qualify_rejected', 0)} failed qualify)"),
            f"- **Classifier:** {clf.get('mode', '?')}"
            + (f", {clf.get('decide_calls', 0)} decisions, {clf.get('decide_errors', 0)} errors, "
               f"${clf.get('decide_cost_usd', 0):.4f}" if clf.get("mode") == "decide" else ""),
        ]
        failing = [sid for sid, s in (r.get("sources") or {}).items()
                   if isinstance(s, dict) and s.get("status") == "error"]
        if failing:
            lines.append(f"- **Sources failing:** {', '.join(failing)}")
    if life:
        lines.append(f"- **Lifecycle:** {life.get('checked', 0)} checked, {life.get('failed', 0)} failed, "
                     f"{life.get('unsure', 0)} unsure (blocked, busy or timed out; not counted), "
                     f"{life.get('revived', 0)} revived, {life.get('sent_to_review_for_drift', 0)} sent to "
                     f"review for drift")
        look = life.get("needs_look") or []
        if look:
            lines.append(f"- **Needs a look** ({len(look)}: unreachable from the checker for several runs, "
                         "still listed):")
            lines += [f"  - [{x.get('title') or x['url']}]({x['url']}): {x['reason']}, {x['checks']} checks"
                      for x in look[:20]]
    if th:
        lines.append(f"- **Thumbnails:** {th.get('made', 0)} preview images, {th.get('screenshots', 0)} screenshots, "
                     f"{th.get('failed', 0)} failed")
    lines.append(f"- **Now:** {published} items stored, **{waiting} waiting for review**")
    if clf.get("mode") == "rules":
        lines += ["", "> OPENROUTER_API_KEY is not set: rules-only classification, nothing auto-publishes."]
    return "\n".join(lines) + "\n"
