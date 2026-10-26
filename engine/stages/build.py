"""Stage 5 — Build: merge layers and write the published files (the data contract)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from engine.config import load_json, write_json
from engine.store import Store

log = logging.getLogger("engine")

CONTRACT_VERSION = 1


def merge_layers(pack, store: Store) -> dict[str, dict[str, Any]]:
    """scraped/enriched item file -> overrides -> merged item."""
    return store.load_items()


def run(ctx, out_dir: Path, dry_run: bool = False) -> dict:
    pack = ctx.pack
    items = merge_layers(pack, ctx.store)
    # A human 'hidden' flag (overrides.yml) takes an item off the site without
    # deleting it; it used to be honoured only when merging, never at build.
    active = {i: it for i, it in items.items()
              if it.get("status") == "active" and "hidden" not in (it.get("flags") or [])}

    # Build runs standalone, so source health comes from the last run's report.
    if not ctx.source_results:
        report = load_json(pack.private_dir / "last_report.json") or {}
        ctx.source_results = report.get("sources", {})
        for result in ctx.source_results.values():
            if isinstance(result, dict):
                result.setdefault("checked", report.get("date", ""))

    today = ctx.today
    # Changelog first: its content hashes drive the Updated / Back-online markers.
    changelog = _changelog(ctx, pack, items, active)
    markers = _markers(ctx, pack, changelog, active)
    thumbs = _thumb_ids(pack, active)
    catalogue_items = [_public_item(it, markers.get(iid, []), iid in thumbs) for iid, it in active.items()]
    catalogue_items.sort(key=lambda x: x["dates"].get("first_seen", ""), reverse=True)

    catalogue = {
        "items": catalogue_items,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # The data version covers the items only: it used to include generated_at,
    # so it changed on every build and could not say whether the data had.
    body = json.dumps(catalogue_items, ensure_ascii=False, sort_keys=True)
    version_hash = hashlib.sha256(body.encode()).hexdigest()[:12]
    # Code and data version separately: the code hash names the asset URLs and
    # the service-worker cache, so a code-only change still reaches browsers.
    code_hash = _code_hash(pack)

    taxonomy = pack.taxonomy
    manifest = {
        "pack": pack.id,
        "name": pack.pack.get("name", pack.id),
        "tagline": pack.pack.get("tagline", ""),
        "noun": pack.pack.get("noun", {"one": "item", "many": "items"}),
        "contract_version": CONTRACT_VERSION,
        "version_hash": version_hash,
        "code_hash": code_hash,
        "generated_at": catalogue["generated_at"],
        "counts": {
            "active": len(catalogue_items),
            "total": len(items),
            "stale": sum(1 for i in items.values() if i.get("status") == "stale"),
            "dead": sum(1 for i in items.values() if i.get("status") == "dead"),
        },
        "facets": _facet_values(pack, catalogue_items),
        "taxonomy": taxonomy,
        "copy": pack.copy.get("strings", {}),
        # theme.yml's default_color_mode reached nothing before: the frontend reads it here.
        "theme": {"css": "theme.css",
                  "default_color_mode": pack.theme_yml.get("default_color_mode", "system")},
        # `sources` in views publishes the Sources page (and sources.json); without it
        # the source list stays with the maintainer (`engine sources-page`).
        "views": pack.pack.get("views", ["grid"]),
        "outputs": list(pack.outputs),
        "card_layout": pack.pack.get("card_layout", "tile"),
        "quick_filters": pack.facets.get("quick_filters", []),
        "sorts": pack.facets.get("sorts", ["newest"]),
        "license": pack.pack.get("license", {}),
        "links": pack.pack.get("links", {}),
        "analytics": pack.pack.get("analytics") or None,
    }

    public_sources = "sources" in pack.pack.get("views", ["grid"])
    sources_public = _sources_json(pack, ctx.source_results, items) if public_sources else None

    written: list[str] = []
    removed: list[str] = []
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        produced: set[Path] = set()

        def out_json(rel: str, data: Any) -> None:
            write_json(out_dir / rel, data)
            produced.add(out_dir / rel)

        out_json("manifest.json", manifest)
        out_json("catalogue.json", catalogue)
        if sources_public is not None:
            out_json("sources.json", sources_public)
        # Always written: index.html links it, and a pack without one 404'd.
        (out_dir / "theme.css").write_text(pack.theme_css or "/* no pack theme */\n",
                                           encoding="utf-8")
        produced.add(out_dir / "theme.css")
        similar = _similar(catalogue_items)
        public = {i["id"]: i for i in catalogue_items}
        for iid, it in active.items():
            out_json(f"items/{iid}.json", _detail_item(it, similar.get(iid, []), public[iid]["image"]))
        # changelog: what changed today, kept in data/ and published for the digest
        write_json(pack.data_dir / "changelog" / f"{today}.json", changelog)
        for iid in thumbs:  # self-hosted thumbnails from `engine thumbs`
            dst = out_dir / "img" / f"{iid}.webp"
            dst.parent.mkdir(exist_ok=True)
            shutil.copy2(pack.data_dir / "img" / f"{iid}.webp", dst)
            produced.add(dst)
        for rel, data in _published_changelog(pack):
            out_json(rel, data)
        if _wants_feeds(pack):
            produced |= _feeds(pack, out_dir, catalogue_items)
        produced |= _copy_site(out_dir, code_hash)
        _stamp_index(out_dir / "index.html", pack)
        removed = _remove_stale_outputs(out_dir, produced)
        written.extend(["manifest.json", "catalogue.json", *(["sources.json"] if sources_public else []),
                        "items/", "changelog/", *(["feeds/"] if _wants_feeds(pack) else []), "site/"])

    report = {
        "pack": pack.id,
        "date": today,
        "dry_run": dry_run,
        "counts": manifest["counts"],
        "version_hash": version_hash,
        "code_hash": code_hash,
        "sources": ctx.source_results,
        "written": written,
        "removed_stale": removed,
    }
    return report


SITE_DIR = Path(__file__).resolve().parent.parent.parent / "site"
BUILD_LIST = ".build-files.json"


def _code_hash(pack) -> str:
    """Hash of the frontend source and the pack theme: changes iff shipped code does."""
    h = hashlib.sha256()
    for f in sorted(p for p in SITE_DIR.rglob("*") if p.is_file()):
        h.update(f.relative_to(SITE_DIR).as_posix().encode())
        h.update(f.read_bytes())
    h.update((pack.theme_css or "").encode())
    return h.hexdigest()[:12]


def _remove_stale_outputs(out_dir: Path, produced: set[Path]) -> list[str]:
    """Delete files a previous build wrote that this build did not (a module
    removed from site/, an item that went dead). Only files listed in the
    previous build's own list are touched, so anything else in the output
    folder (a CNAME file, say) is left alone."""
    list_path = out_dir / BUILD_LIST
    previous = load_json(list_path) or []
    now = sorted(p.relative_to(out_dir).as_posix() for p in produced)
    removed = []
    for rel in sorted(set(previous) - set(now)):
        target = (out_dir / rel).resolve()
        if out_dir.resolve() in target.parents and target.is_file():
            target.unlink()
            removed.append(rel)
    write_json(list_path, now)
    return removed


def _copy_site(out_dir: Path, version_hash: str = "") -> set[Path]:
    """Copy the static frontend (site/) into the build output, no build step.
    Returns the output paths it wrote."""
    import shutil
    src = SITE_DIR
    if not src.is_dir():
        return set()
    dst = out_dir
    shutil.copy2(src / "index.html", dst / "index.html")
    if (src / "app.js").exists():
        shutil.copy2(src / "app.js", dst / "app.js")
    if (src / "site.webmanifest").exists():
        shutil.copy2(src / "site.webmanifest", dst / "site.webmanifest")
    if (src / "sw.js").exists():
        sw = (src / "sw.js").read_text(encoding="utf-8")
        # Stamp the cache version with the build hash so each rebuild gets a fresh
        # offline cache and old copies are purged on activate.
        if version_hash:
            sw = re.sub(r"const VERSION = '[^']*';",
                        f"const VERSION = 'v4-{version_hash}';", sw, count=1)
        (dst / "sw.js").write_text(sw, encoding="utf-8")
    for sub in ("js", "css", "default-theme"):
        if (src / sub).is_dir():
            shutil.copytree(src / sub, dst / sub, dirs_exist_ok=True)
    if version_hash:
        _version_asset_urls(dst, version_hash)
    return {dst / f.relative_to(src) for f in src.rglob("*") if f.is_file()}


# Relative ES-module specifiers: `from './x.js'`, `import './x.js'`, `import('./x.js')`.
_IMPORT_RE = re.compile(r"""((?:\bfrom|\bimport)\s*\(?\s*)(['"])(\.{1,2}/[^'"?]+\.js)\2""")
# Shell assets referenced from index.html.
_HTML_ASSET_RE = re.compile(r"""((?:src|href)=")(\./[^"?]+\.(?:js|css))(")""")


def _stamp_index(index: Path, pack) -> None:
    """index.html is shared by every pack; give it this pack's title and description
    (what a link preview or a search engine sees before any JavaScript runs)."""
    import html as html_lib

    strings = pack.copy.get("strings", {})
    title = html_lib.escape(strings.get("site_title") or pack.pack.get("name", pack.id))
    desc = html_lib.escape(pack.pack.get("tagline", ""), quote=True)
    text = index.read_text(encoding="utf-8")
    text = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", text, count=1)
    text = re.sub(r'(<meta name="description" content=")[^"]*(")', rf"\g<1>{desc}\g<2>", text, count=1)
    # Feed discovery tags only for the feeds this pack actually publishes.
    if "rss" not in pack.outputs:
        text = re.sub(r'\s*<link rel="alternate" type="application/rss\+xml"[^>]*>', "", text)
    if "jsonfeed" not in pack.outputs:
        text = re.sub(r'\s*<link rel="alternate" type="application/feed\+json"[^>]*>', "", text)
    index.write_text(text, encoding="utf-8")


def _wants_feeds(pack) -> bool:
    return bool({"rss", "jsonfeed"} & set(pack.outputs))


def _version_asset_urls(dst: Path, version_hash: str) -> None:
    """Append ?v=<hash> to every module import and shell asset URL in the build.

    Static hosts send no or short Cache-Control headers (python's http.server none
    at all, GitHub Pages max-age=600), so browsers reuse cached modules by
    heuristic. A returning visitor then runs a mix of old and new modules — a new
    app.js importing a name an old data.js lacks is a blank page. Versioned URLs
    make each build's modules a separate, all-or-nothing set.
    """
    suffix = f"?v={version_hash}"
    for js in [dst / "app.js", *(dst / "js").rglob("*.js")]:
        if js.is_file():
            text = js.read_text(encoding="utf-8")
            js.write_text(_IMPORT_RE.sub(lambda m: f"{m[1]}{m[2]}{m[3]}{suffix}{m[2]}", text),
                          encoding="utf-8")
    index = dst / "index.html"
    html = index.read_text(encoding="utf-8")
    index.write_text(_HTML_ASSET_RE.sub(lambda m: f"{m[1]}{m[2]}{suffix}{m[3]}", html),
                     encoding="utf-8")


def _public_item(it: dict[str, Any], markers: list[str] | None = None,
                 thumb: bool = False) -> dict[str, Any]:
    image = it.get("image") if "no-image" not in (it.get("flags") or []) else None
    if image and image.get("kind") == "screenshot" and not thumb:
        image = None  # a screenshot exists only as our file; its src is the app, not a picture
    if image and thumb:
        # Our own copy; the original stays as `origin` for credit and refresh.
        image = {"src": f"img/{it['id']}.webp", "alt": image.get("alt", ""),
                 "width": 640, "height": 360, "origin": image.get("origin") or image.get("src")}
        if it["image"].get("kind") == "screenshot":
            image |= {"kind": "screenshot", "taken": it["image"].get("taken", "")}
    return {
        "id": it["id"],
        "url": it["url"],
        "title": it.get("title", ""),
        "summary": it.get("summary", ""),
        "why": it.get("why", ""),
        "category": it.get("category", ""),
        "subcategory": it.get("subcategory", ""),
        "tags": it.get("tags", []),
        "attrs": it.get("attrs", {}),
        "dates": it.get("dates", {}),
        "status": it.get("status", "active"),
        "ai_claim": it.get("ai_claim", "none"),
        "flags": it.get("flags", []),
        "quality": {k: it.get("quality", {}).get(k) for k in ("confidence", "mentions", "pick")},
        "creator": it.get("creator"),
        "image": image,
        "sources": sorted({p.get("source", "") for p in it.get("provenance", [])} - {""}),
        "markers": markers or [],
    }


def _content_hash(it: dict[str, Any]) -> str:
    """What a visitor would notice changing (not liveness or verification dates)."""
    keys = ("url", "title", "summary", "why", "category", "subcategory", "tags", "attrs", "ai_claim")
    body = json.dumps({k: it.get(k) for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(body.encode()).hexdigest()[:12]


def _markers(ctx, pack, today_log: dict, active: dict, days: int = 7) -> dict[str, list[str]]:
    """'updated' if an item's content changed in the last `days` changelogs;
    'back-online' if lifecycle revived it within `days`. 'New' stays client-side
    (it comes straight from dates.first_seen)."""
    changed: set[str] = {c["id"] for c in today_log.get("changed", [])}
    chdir = pack.data_dir / "changelog"
    if chdir.exists():
        for f in sorted(chdir.glob("*.json")):
            if f.stem < ctx.today and ctx.today_str_diff(f.stem) < days:
                changed |= {c["id"] for c in (load_json(f) or {}).get("changed", [])}
    out: dict[str, list[str]] = {}
    for iid, it in active.items():
        m = []
        if iid in changed:
            m.append("updated")
        if ctx.today_str_diff((it.get("dates") or {}).get("back_online", "")) < days:
            m.append("back-online")
        if m:
            out[iid] = m
    return out


def _thumb_ids(pack, active: dict) -> set[str]:
    folder = pack.data_dir / "img"
    return {iid for iid, it in active.items()
            if (folder / f"{iid}.webp").exists() and it.get("image")
            and "no-image" not in (it.get("flags") or [])}


# Working fields that never belong in a published file (page text, raw source
# payloads, liveness bookkeeping).
_PRIVATE_FIELDS = ("text", "raw", "liveness", "reasons", "tier", "source")


def _detail_item(it: dict[str, Any], similar: list[str] | None = None,
                 image: dict | None = None) -> dict[str, Any]:
    d = {k: v for k, v in it.items() if k not in _PRIVATE_FIELDS + ("fingerprint",)}
    d["image"] = image  # the published image (our thumbnail when there is one), never a takedown
    d["provenance"] = it.get("provenance", [])
    d["evidence"] = it.get("evidence", [])
    d["similar"] = similar or []
    return d


def _similar(items: list[dict], limit: int = 4) -> dict[str, list[str]]:
    """Precomputed similar items: shared tags count 2, same subcategory 2, same
    category 1; at least a score of 2 so a bare category match isn't 'similar'."""
    out: dict[str, list[str]] = {}
    for a in items:
        tags = set(a.get("tags") or [])
        scored = []
        for b in items:
            if b["id"] == a["id"]:
                continue
            score = 2 * len(tags & set(b.get("tags") or []))
            if a.get("subcategory") and a.get("subcategory") == b.get("subcategory"):
                score += 2
            if a.get("category") and a.get("category") == b.get("category"):
                score += 1
            if score >= 2:
                scored.append((-score, b.get("title", ""), b["id"]))
        out[a["id"]] = [i for _, _, i in sorted(scored)[:limit]]
    return out


def _published_changelog(pack, days: int = 60) -> list[tuple[str, dict]]:
    """The last `days` changelog files, without the internal active-id list, plus
    an index the digest view reads. Empty days are left out of the index."""
    chdir = pack.data_dir / "changelog"
    files = sorted(chdir.glob("*.json"))[-days:] if chdir.exists() else []
    out, index = [], []
    for f in reversed(files):
        data = load_json(f) or {}
        public = {k: v for k, v in data.items() if k not in ("active_ids", "hashes")}
        out.append((f"changelog/{f.stem}.json", public))
        counts = public.get("counts", {})
        if counts.get("added") or counts.get("removed") or counts.get("changed"):
            index.append({"date": f.stem, "added": counts.get("added", 0),
                          "removed": counts.get("removed", 0), "changed": counts.get("changed", 0)})
    out.append(("changelog/index.json", {"days": index}))
    return out


def _facet_values(pack, items: list[dict]) -> list[dict]:
    out = []
    for f in pack.facets.get("facets", []):
        entry = dict(f)
        f["id"]
        ftype = f.get("type")
        if ftype == "tree":
            entry["values"] = sorted({it.get(f["field"][0], "") for it in items} - {""})
        elif ftype in ("enum", "multi"):
            vals = set()
            for it in items:
                v = it.get("attrs", {}).get(f["field"].split(".")[-1]) if "." in f["field"] else it.get(f["field"])
                if isinstance(v, list):
                    vals.update(v)
                elif v:
                    vals.add(v)
            entry["values"] = sorted(vals)
        elif ftype == "bool":
            entry["values"] = [True, False]
        out.append(entry)
    return out


def _sources_json(pack, source_results: dict, items: dict[str, dict]) -> dict:
    counts: dict[str, int] = {}
    for it in items.values():
        for p in it.get("provenance", []):
            counts[p["source"]] = counts.get(p["source"], 0) + 1
    sources = []
    for src in pack.sources:
        sid = src["id"]
        sources.append({
            "id": sid,
            "type": src.get("type"),
            "trust": src.get("trust", "B"),
            "status": src.get("status", "active"),
            "notes": src.get("notes", ""),
            "last_checked": (source_results.get(sid, {}) or {}).get("checked", ""),
            "result": source_results.get(sid, {}),
            "items_contributed": counts.get(sid, 0),
        })
    return {"sources": sources, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _changelog(ctx, pack, items: dict, active: dict) -> dict:
    # Diff against the previous changelog — yesterday's when it exists, else the
    # most recent earlier one, so a skipped day does not mark everything "added".
    chdir = pack.data_dir / "changelog"
    prev_ids: set[str] = set()
    prev_hashes: dict[str, str] = {}
    if chdir.exists():
        prior = sorted(p for p in chdir.glob("*.json") if p.stem < ctx.today)
        if prior:
            try:
                prev = json.loads(prior[-1].read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                prev = {}
            prev_ids = set(prev.get("active_ids", []))
            prev_hashes = prev.get("hashes", {})
    now_ids = set(active)
    added = sorted(now_ids - prev_ids)
    removed = sorted(prev_ids - now_ids)
    hashes = {i: _content_hash(active[i]) for i in sorted(now_ids)}
    # Changed = present before and now, with different visible content. The first
    # changelog after this field existed has no hashes to compare, so nothing is.
    changed = sorted(i for i in now_ids & prev_ids if i in prev_hashes and prev_hashes[i] != hashes[i])
    return {
        "date": ctx.today,
        "added": [{"id": i, "title": active[i].get("title", "")} for i in added],
        "removed": [{"id": i, "title": items.get(i, {}).get("title", "")} for i in removed],
        "changed": [{"id": i, "title": active[i].get("title", "")} for i in changed],
        "counts": {"added": len(added), "removed": len(removed), "changed": len(changed),
                   "active": len(now_ids)},
        "active_ids": sorted(now_ids),
        "hashes": hashes,
    }


def _feeds(pack, out_dir: Path, items: list[dict]) -> set[Path]:
    """The whole catalogue's feeds, plus one pair per category (feeds/<category>.xml
    and .json) so people can follow just what they care about."""
    written = _feed_pair(pack, out_dir, items, "rss.xml", "feed.json")
    for cat in sorted({it.get("category") for it in items} - {"", None}):
        label = pack.categories.get(cat, {}).get("label", cat)
        written |= _feed_pair(pack, out_dir, [it for it in items if it.get("category") == cat],
                              f"{cat}.xml", f"{cat}.json", f" · {label}")
    return written


def _feed_pair(pack, out_dir: Path, items: list[dict], rss_name: str, json_name: str,
               suffix: str = "") -> set[Path]:
    written: set[Path] = set()
    (out_dir / "feeds").mkdir(exist_ok=True)
    if "rss" in pack.outputs:
        (out_dir / "feeds" / rss_name).write_text(_rss_xml(pack, items, suffix), encoding="utf-8")
        written.add(out_dir / "feeds" / rss_name)
    if "jsonfeed" in pack.outputs:
        feed = _json_feed(pack, items, suffix)
        (out_dir / "feeds" / json_name).write_text(
            json.dumps(feed, ensure_ascii=False, indent=1), encoding="utf-8")
        written.add(out_dir / "feeds" / json_name)
    return written


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rss_xml(pack, items: list[dict], suffix: str = "") -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0"><channel>',
             f"<title>{_esc(pack.pack.get('name', pack.id) + suffix)}</title>",
             f"<description>{_esc(pack.pack.get('tagline', ''))}</description>",
             "<link>./</link>"]
    for it in items[:200]:
        lines.append(
            f"<item><title>{_esc(it['title'])}</title>"
            f"<link>{_esc(it['url'])}</link>"
            f"<description>{_esc(it.get('summary', ''))}</description>"
            f"<guid>{_esc(it['id'])}</guid></item>")
    lines.append("</channel></rss>")
    return "\n".join(lines)


def _json_feed(pack, items: list[dict], suffix: str = "") -> dict:
    return {
        "version": "https://jsonfeed.org/version/1.1",
        "title": pack.pack.get("name", pack.id) + suffix,
        "description": pack.pack.get("tagline", ""),
        "home_page_url": "./",
        "items": [
            {"id": it["id"], "url": it["url"], "title": it["title"],
             "content_text": it.get("summary", ""),
             "date_published": it.get("dates", {}).get("first_seen", ""),
             "tags": it.get("tags", [])}
            for it in items[:200]
        ],
    }
