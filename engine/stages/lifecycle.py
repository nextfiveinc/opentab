"""Lifecycle pass: re-check published items, mark stale/dead, revive, catch drift."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from engine.config import write_json
from engine.http import fetch
from engine.stages.enrich import extract_page_meta, preview_image

log = logging.getLogger("engine")

_WORD = re.compile(r"[a-z0-9]{3,}")


def fingerprint_from_raw(raw: dict) -> dict | None:
    """Fingerprint at publish time, from the metadata qualify already captured."""
    meta = (raw or {}).get("page_meta")
    if not meta:
        return None
    host = ((raw or {}).get("signals") or {}).get("final_host", "")
    return fingerprint(meta, f"https://{host}/" if host else "")


def fingerprint(meta: dict, final_url: str) -> dict:
    """What identifies an app's page: where it ends up and what it says it is."""
    return {
        "host": urlsplit(final_url).netloc.lower().removeprefix("www.") if final_url else "",
        "title": (meta.get("title") or "")[:200],
        "site_name": (meta.get("site_name") or "")[:120],
        "description": (meta.get("description") or "")[:300],
    }


def _words(fp: dict) -> set[str]:
    return set(_WORD.findall(f"{fp.get('title', '')} {fp.get('site_name', '')} {fp.get('description', '')}".lower()))


def _site(host: str) -> str:
    """Rough registrable domain: the last two labels (three for short second levels
    like co.uk), so moving between subdomains of one site is not drift."""
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-2]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def drift(old: dict, new: dict) -> str | None:
    """Reason the page no longer looks like the listed app, or None.

    Re-registered expired domains are how link catalogues end up pointing at spam:
    the host redirecting elsewhere, or the same host now describing something
    unrelated. (The old check — title substring vs summary length — caught neither.)
    """
    if old.get("host") and new.get("host") and _site(old["host"]) != _site(new["host"]):
        return f"host-changed:{old['host']}->{new['host']}"
    a, b = _words(old), _words(new)
    if len(a) >= 3 and len(b) >= 3 and len(a & b) / len(a | b) < 0.15:
        return "content-changed"
    return None


# A failed check is only evidence that the app is gone when the answer is definite (404,
# a domain that no longer resolves). Being blocked or rate-limited, a server hiccup or a
# timeout says more about the checker (a data-centre address, a busy hour) than the
# app; apps that load fine elsewhere were failing from GitHub's runners. Those are
# recorded but never count towards stale or dead. After a run of them the item stays
# listed and is named in the run summary ("needs a look"), so a person decides.
_UNSURE_ERRORS = {"blocked-by-robots", "ReadTimeout", "ConnectTimeout", "WriteTimeout",
                  "PoolTimeout", "TimeoutException", "RemoteProtocolError"}


def failure_reason(result) -> tuple[str, bool]:
    """(reason, unsure) for a failed fetch: 'http-404', 'ConnectError', …"""
    if result.error:
        return result.error, result.error in _UNSURE_ERRORS
    status = result.status
    return f"http-{status}", status in (401, 403, 408, 425, 429) or status >= 500


def _due(ctx, it: dict, cfg: dict, everything: bool) -> bool:
    if everything or it.get("status") != "active":
        return True
    last = (it.get("liveness") or {}).get("last_check") or it.get("dates", {}).get("last_verified", "")
    if not last:
        return True
    # Plan cadence: daily during an item's first week, then weekly.
    new_days = int(cfg.get("new_item_days", 7))
    is_new = ctx.today_str_diff(it.get("dates", {}).get("first_seen", "")) < new_days
    every = int(cfg.get("new_item_recheck_days", 1)) if is_new else int(cfg.get("recheck_days", 7))
    return ctx.today_str_diff(last) >= every


def run(ctx, recheck_days: int | None = None, everything: bool = False) -> dict:
    """Re-check items that are due. `everything` re-checks every non-dead item now."""
    pack = ctx.pack
    cfg = dict(pack.pack.get("lifecycle", {}))
    if recheck_days:
        cfg["recheck_days"] = recheck_days
    stale_after = int(cfg.get("stale_after_failures", 2))
    dead_after = int(cfg.get("dead_after_failures", 4))
    unsure_flag_after = int(cfg.get("unsure_flag_after", 4))

    items = ctx.store.load_items()
    checked = failed = unsure = revived = drifted = 0
    needs_look: list[dict] = []
    for iid, it in list(items.items()):
        if it.get("status") == "dead" or not _due(ctx, it, cfg, everything):
            continue
        it = ctx.store.load_item(iid, apply_overrides=False) or it  # never bake overrides in
        with ctx.http_client() as client:
            result = fetch(it["url"], client)
        checked += 1
        liveness = it.setdefault("liveness", {})
        if result.ok:
            meta = extract_page_meta(result.body or "")
            fp = fingerprint(meta, result.url)
            if not it.get("fingerprint"):
                it["fingerprint"] = fp  # baseline for items published before fingerprints
            reason = drift(it["fingerprint"], fp)
            if reason:
                _send_to_review(ctx, it, reason, fp)
                drifted += 1
                continue
            if not it.get("image"):
                image = preview_image(meta, it["url"], it.get("title", ""))
                if image:
                    it["image"] = image
            if it.get("status") == "stale":
                it["status"] = "active"
                it.setdefault("dates", {})["back_online"] = ctx.today
                revived += 1
                log.info("revived %s", iid)
            liveness["failures"] = 0
            for k in ("unsure", "reason"):
                liveness.pop(k, None)
            # last_verified drives the "Opened and working on" badge: success only.
            it.setdefault("dates", {})["last_verified"] = ctx.today
        else:
            reason, is_unsure = failure_reason(result)
            liveness["reason"] = reason
            if is_unsure:
                liveness["unsure"] = liveness.get("unsure", 0) + 1
                unsure += 1
                if liveness["unsure"] >= unsure_flag_after:
                    needs_look.append({"id": iid, "title": it.get("title", ""), "url": it["url"],
                                       "reason": reason, "checks": liveness["unsure"]})
            else:
                liveness["failures"] = liveness.get("failures", 0) + 1
                failed += 1
                if liveness["failures"] >= dead_after:
                    it["status"] = "dead"
                elif liveness["failures"] >= stale_after:
                    it["status"] = "stale"
        liveness["last_check"] = ctx.today
        ctx.store.save_item(it)

    report = {"checked": checked, "failed": failed, "unsure": unsure, "revived": revived,
              "sent_to_review_for_drift": drifted, "needs_look": needs_look, "date": ctx.today}
    log.info("lifecycle: checked=%d failed=%d unsure=%d revived=%d drifted=%d needs_look=%d",
             checked, failed, unsure, revived, drifted, len(needs_look))
    return report


def _send_to_review(ctx, it: dict, reason: str, new_fp: dict) -> None:
    """Take a drifted item off the site and put it in front of a human: it moves to
    data/review/ with the old and new fingerprints; `approve` re-publishes it with
    the new fingerprint, `reject` drops it."""
    log.warning("drift on %s: %s", it["id"], reason)
    it["status"] = "review"
    it["flags"] = [*[f for f in it.get("flags", []) if f != "content-drift"], "content-drift"]
    it.setdefault("quality", {})["needs"] = "drift-check"
    it["quality"]["drift"] = {"reason": reason, "was": it.get("fingerprint"), "now": new_fp,
                              "seen": ctx.today}
    write_json(ctx.pack.private_dir / "review" / f"{it['id']}.json", it)
    ctx.store.remove_item(it["id"])

