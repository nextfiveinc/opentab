"""Review that scales (P2): a local review page, a decisions file, and bulk rules.

  engine review-page <pack> [--decisions f …]  write review/index.html (self-contained, local),
                                                pre-loaded with earlier exports
  engine apply-decisions <pack> <file> …        apply one or more exported decisions files
  engine bulk <pack> --reject-below …   decide many items by rule (dry run unless --yes)
  engine sources-page <pack>            write review/sources.html: where the engine looks

The page embeds the queue and never needs a server, so it works from file://. It is
not part of the published site: it shows page text and lives in the gitignored
review/ folder.

Long queues take several sittings. The page exports its decisions at any point and
imports an earlier export (or several), merging them: per item, the decision made
last wins (each carries an `at` timestamp; a file without them counts as oldest).
Clearing a decision is recorded too (`action: "clear"`), so an older export cannot
bring it back. Decisions for items that have left the queue are kept and exported, so
nothing is lost; apply-decisions reports them as missing.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

REJECT_REASONS = ["not-a-web-app", "paid-only", "broken", "login-wall", "chat-wrapper",
                  "duplicate", "low-quality", "off-topic", "other"]


def queue(pack) -> list[dict[str, Any]]:
    rows = []
    for f in sorted((pack.private_dir / "review").glob("*.json")):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        q = r.get("quality") or {}
        meta = (r.get("raw") or {}).get("page_meta") or {}
        rows.append({
            "id": f.stem, "url": r.get("url", ""), "title": r.get("title", ""),
            "summary": r.get("summary", ""), "why": r.get("why", ""),
            "description": meta.get("description", ""),
            "category": r.get("category", ""), "tier": r.get("tier", ""),
            "sources": sorted({p.get("source", "") for p in r.get("provenance", [])}),
            "found": [p.get("url") for p in r.get("provenance", []) if p.get("url")][:3],
            "confidence": round(float(q.get("confidence", 0) or 0), 3),
            "mode": q.get("classify_mode", ""), "decisions": q.get("decisions", {}),
            "model": q.get("decide_model", ""), "needs": q.get("needs", ""),
            "drift": (q.get("drift") or {}).get("reason", ""),
            "disagreements": q.get("disagreements", []),
            "held": _held_note(q.get("held_for_human")),
            "ai_claim": r.get("ai_claim", "none"), "attrs": r.get("attrs", {}),
            "first_seen": (r.get("dates") or {}).get("first_seen", ""),
            "excerpt": (r.get("text") or "")[:600],
        })
    rows.sort(key=lambda r: (-r["confidence"], r["title"]))
    return rows


def _held_note(held: dict | None) -> str:
    """Why an item is in review although the gate decided otherwise (reclassify --keep)."""
    if not held:
        return ""
    verb = "reject" if held.get("would_be") == "rejected" else held.get("would_be", "")
    reasons = f" ({', '.join(held['reasons'])})" if held.get("reasons") else ""
    return f"held for you: the gate would {verb}{reasons}"


def write_page(ctx, out: Path, seed: list[str] | None = None) -> dict:
    rows = queue(ctx.pack)
    earlier = merge_decisions(*(read_decisions(p) for p in seed or []))
    data = json.dumps({"pack": ctx.pack.id, "generated": ctx.today, "reasons": REJECT_REASONS,
                       "categories": {k: v.get("label", k) for k, v in ctx.pack.categories.items()},
                       "items": rows, "seed": earlier}, ensure_ascii=False).replace("</", "<\\/")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.replace("__DATA__", data).replace("__TITLE__", html.escape(
        f"Review queue · {ctx.pack.pack.get('name', ctx.pack.id)}")), encoding="utf-8")
    write_sources_page(ctx, out.parent / "sources.html")   # the page links to it
    ids = {r["id"] for r in rows}
    return {"written": str(out), "items": len(rows),
            "preloaded": sum(1 for d in earlier if d["id"] in ids and d.get("action") != "clear"),
            "next": f"open it, decide (export any time), then: "
                    f"uv run python -m engine apply-decisions {ctx.pack.path} <decisions.json>"}


def read_decisions(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("decisions", [])
    return [d for d in rows if isinstance(d, dict) and d.get("id")]


def merge_decisions(*groups: list[dict]) -> list[dict]:
    """One decision per id: the latest `at` wins; on a tie (or no timestamps) the later
    group wins. Same rule as the page's import, so CLI and browser agree."""
    best: dict[str, dict] = {}
    for group in groups:
        for d in group:
            cur = best.get(d["id"])
            if cur is None or str(d.get("at") or "") >= str(cur.get("at") or ""):
                best[d["id"]] = d
    return list(best.values())


def write_sources_page(ctx, out: Path) -> dict:
    """The maintainer's Sources page: every source, when it was last checked, and how
    many published items it found. Local only (gitignored review/), like the review
    page; the public site leaves sources out unless the pack lists `sources` in views."""
    from engine.config import load_json
    from engine.stages.build import _sources_json

    report = load_json(ctx.pack.private_dir / "last_report.json") or {}
    results = {sid: {**r, "checked": r.get("checked") or report.get("date", "")}
               for sid, r in (report.get("sources") or {}).items() if isinstance(r, dict)}
    data = _sources_json(ctx.pack, results, ctx.store.load_items())["sources"]
    strings = ctx.pack.copy.get("strings", {})
    rows = []
    for s in data:
        res = s.get("result") or {}
        note = html.escape(s.get("notes") or "")
        if s.get("status") == "paused":
            note += ' <span class="warn">Paused.</span>'
        elif res.get("status") == "error":
            note += f' <span class="warn">Last check failed: {html.escape(str(res.get("error", ""))[:160])}</span>'
        checked = html.escape((s.get("last_checked") or "")[:10]) or "never"
        rows.append(f"<tr><td><strong>{html.escape(s['id'])}</strong><small>{note}</small></td>"
                    f"<td>{checked}</td><td class=num>{int(s.get('items_contributed') or 0)}</td></tr>")
    title = strings.get("sources_page", "Sources")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(SOURCES_PAGE.format(
        title=html.escape(f"{title} · {ctx.pack.pack.get('name', ctx.pack.id)}"), h1=html.escape(title),
        intro=html.escape(strings.get("sources_intro", "")), rows="\n".join(rows),
        generated=html.escape(ctx.today)), encoding="utf-8")
    return {"written": str(out), "sources": len(rows)}


def apply_decisions(pack_path: str, path: str | list[str], dry_run: bool = False) -> dict:
    from engine import pipeline

    paths = [path] if isinstance(path, str) else list(path)
    decisions = merge_decisions(*(read_decisions(p) for p in paths))
    done = {"approved": [], "rejected": [], "skipped": [], "missing": []}
    for d in decisions:
        iid, action = d.get("id", ""), d.get("action", "")
        if action not in ("approve", "reject"):
            done["skipped"].append(iid)
            continue
        if dry_run:
            exists = (pipeline._context(pack_path).pack.private_dir / "review" / f"{iid}.json").exists()
            done[("approved" if action == "approve" else "rejected") if exists else "missing"].append(iid)
            continue
        res = (pipeline.cmd_approve(pack_path, iid) if action == "approve"
               else pipeline.cmd_reject(pack_path, iid, reason=d.get("reason") or "human-reject"))
        ok = res.get("approved") or res.get("rejected")
        done[("approved" if action == "approve" else "rejected") if ok else "missing"].append(iid)
    return {"dry_run": dry_run, **{k: len(v) for k, v in done.items()},
            "missing_ids": done["missing"][:20]}


def bulk(pack_path: str, reject_below: float | None = None, approve_at: float | None = None,
         tiers: list[str] | None = None, category: str | None = None, yes: bool = False,
         apply_rules: bool = False) -> dict:
    """Decide by rule. Approvals only trust AI confidence (decide or llm mode): the
    rules-mode number is a fixed placeholder, so it can reject but never approve.
    `apply_rules` rejects queued items that today's rules.yml exclusions match (the
    queue may predate a rule), logging the rule's own reason."""
    from engine import pipeline
    from engine.models import Candidate
    from engine.stages.qualify import rules_match

    ctx = pipeline._context(pack_path)
    plan: dict[str, list[dict]] = {"approve": [], "reject": []}
    for row in queue(ctx.pack):
        if apply_rules:
            reason = rules_match(ctx.pack, Candidate(url=row["url"], title=row["title"], text=row["excerpt"]))
            if reason:
                plan["reject"].append({**row, "reason": reason})
                continue
        if tiers and row["tier"] not in tiers:
            continue
        if category and row["category"] != category:
            continue
        if row["needs"] == "drift-check":
            continue  # drifted items always get a human look
        if approve_at is not None and row["mode"] in ("decide", "llm") and row["confidence"] >= approve_at \
                and not row["disagreements"]:
            plan["approve"].append(row)
        elif reject_below is not None and row["confidence"] < reject_below:
            plan["reject"].append({**row, "reason": f"bulk:confidence<{reject_below}"})
    if yes:
        for row in plan["approve"]:
            pipeline.cmd_approve(pack_path, row["id"])
        for row in plan["reject"]:
            pipeline.cmd_reject(pack_path, row["id"], reason=row["reason"])
    return {"applied": yes, "approve": len(plan["approve"]), "reject": len(plan["reject"]),
            "approve_examples": _brief(plan["approve"]), "reject_examples": _brief(plan["reject"]),
            "note": None if yes else "dry run: add --yes to apply"}


def _brief(rows: list[dict]) -> list[str]:
    return [f"{r['confidence']:.2f}  {r.get('reason', '')[:24]:24}  {r['title'][:50]}  {r['url'][:70]}"
            for r in rows[:15]]


SOURCES_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#f6f3ec;--s:#fffdf8;--t:#1c1a16;--m:#6b655a;--b:#e4ddcf;--a:#d2461f;color-scheme:light dark}}
@media (prefers-color-scheme:dark){{:root{{--bg:#15130f;--s:#1d1a15;--t:#f1ebdf;--m:#a89f8e;--b:#322d24;--a:#ff7b54}}}}
body{{margin:0;background:var(--bg);color:var(--t);font:15px/1.5 ui-sans-serif,system-ui,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:24px 16px 60px}}h1{{font:600 1.6rem Georgia,serif;margin:0 0 4px}}
p{{color:var(--m);margin:0 0 20px}}table{{width:100%;border-collapse:collapse;background:var(--s);border:1px solid var(--b);border-radius:12px;overflow:hidden}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--b);vertical-align:top}}th{{font-size:.8rem;color:var(--m);font-weight:600}}
td small{{display:block;color:var(--m);font-size:.85rem;margin-top:2px}}.num{{text-align:right}}.warn{{color:var(--a)}}
a{{color:inherit}}.foot{{margin-top:16px;font-size:.85rem}}
</style></head><body><main>
<h1>{h1}</h1><p>{intro}</p>
<table><thead><tr><th>Source</th><th>Last checked</th><th class=num>Items</th></tr></thead>
<tbody>
{rows}
</tbody></table>
<p class="foot">Generated {generated} from the last run report. Only on this machine: review/ is never committed or published. <a href="index.html">Review queue</a></p>
</main></body></html>
"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f3ec;--s:#fffdf8;--t:#1c1a16;--m:#6b655a;--b:#e4ddcf;--a:#d2461f;--ok:#2f7a4b;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#15130f;--s:#1d1a15;--t:#f1ebdf;--m:#a89f8e;--b:#322d24;--a:#ff7b54;--ok:#7fd49b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--t);font:15px/1.5 ui-sans-serif,system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--b);padding:12px 20px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
h1{font:600 1.15rem Georgia,serif;margin:0 12px 0 0}a{color:inherit}a.title:hover{color:var(--a)}input,select,button{font:inherit;color:inherit}
input,select{height:34px;border:1px solid var(--b);border-radius:8px;background:var(--s);padding:0 10px}
button{height:34px;padding:0 12px;border-radius:999px;border:1px solid var(--b);background:var(--s);cursor:pointer}
button.ok{border-color:var(--ok);color:var(--ok)}button.no{border-color:var(--a);color:var(--a)}button.primary{background:var(--t);color:var(--bg);border-color:var(--t)}
.stats{margin-left:auto;color:var(--m);font:13px ui-monospace,monospace}
main{max-width:1100px;margin:0 auto;padding:16px 20px 80px;display:grid;gap:12px}
article{background:var(--s);border:1px solid var(--b);border-radius:12px;padding:14px 16px;display:grid;gap:6px}
article.approve{border-left:4px solid var(--ok)}article.reject{border-left:4px solid var(--a);opacity:.65}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.title{font-weight:600;font-size:1.02rem}
.m{color:var(--m);font-size:.88rem}.mono{font:12px ui-monospace,monospace;color:var(--m)}
.chip{font-size:.75rem;border:1px solid var(--b);border-radius:999px;padding:0 8px;color:var(--m)}
.warn{color:var(--a);border-color:var(--a)}.conf{font:600 13px ui-monospace,monospace}
details{font-size:.88rem;color:var(--m)}.actions{margin-left:auto;display:flex;gap:6px}
.bar{flex-basis:100%;display:flex;gap:10px;align-items:center;font-size:.88rem;color:var(--m)}
.bar progress{flex:1;max-width:320px;height:8px;accent-color:var(--ok)}
#note{color:var(--t)}#note.err{color:var(--a)}
body.drop main{outline:2px dashed var(--a);outline-offset:-8px}
</style></head><body>
<header>
  <h1>Review queue</h1>
  <a class="m" href="sources.html">Sources</a>
  <input id="q" type="search" placeholder="Search title, URL, text">
  <label class="m">conf ≥ <input id="min" type="number" step="0.05" min="0" max="1" value="0" style="width:70px"></label>
  <label class="m">≤ <input id="max" type="number" step="0.05" min="0" max="1" value="1" style="width:70px"></label>
  <select id="cat"><option value="">All categories</option></select>
  <select id="state"><option value="">All</option><option value="undecided" selected>Undecided</option><option value="approve">Approved</option><option value="reject">Rejected</option></select>
  <button class="ok" id="bulkOk">Approve visible</button>
  <select id="bulkReason"></select><button class="no" id="bulkNo">Reject visible</button>
  <button id="imp" title="Merge an earlier export into this page (or drop the file on the page)">Import…</button>
  <input id="file" type="file" accept=".json,application/json" multiple hidden>
  <button class="primary" id="dl" title="Save your decisions so far; import them in a later session">Export decisions</button>
  <span class="stats" id="stats"></span>
  <div class="bar"><progress id="prog" max="1" value="0"></progress><span id="done"></span><span id="note"></span></div>
</header>
<main id="list"></main>
<script>
const DATA = __DATA__;
const KEY = 'review-decisions:' + DATA.pack;
// dec: id -> {action: approve|reject|clear, reason, at}. "clear" records an undo, so an
// older export can't bring the decision back. Kept in localStorage between visits, and
// exported/imported as decisions.json between sessions or machines.
let dec = {}; try { dec = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) {}
let dirty = false;
const save = () => { dirty = true; try { localStorage.setItem(KEY, JSON.stringify(dec)); } catch (e) {} draw(); };
const now = () => new Date().toISOString();
const set = (id, action, reason = '') => { dec[id] = { action, reason, at: now() }; };
const decided = (id) => dec[id] && dec[id].action !== 'clear';
const inQueue = new Set(DATA.items.map((it) => it.id));
// Same rule as engine.review.merge_decisions: the decision made last wins; a missing
// timestamp counts as oldest; on a tie the incoming one wins.
function merge(rows) {
  const n = { added: 0, updated: 0, kept: 0, gone: 0 };
  for (const d of rows) {
    if (!d || !d.id || !['approve', 'reject', 'clear'].includes(d.action)) continue;
    const cur = dec[d.id];
    if (cur && String(cur.at || '') > String(d.at || '')) { n.kept++; continue; }
    if (!inQueue.has(d.id)) n.gone++; else if (cur) n.updated++; else n.added++;
    dec[d.id] = { action: d.action, reason: d.reason || '', at: d.at || '' };
  }
  return n;
}
function note(msg, err) { $('note').textContent = msg; $('note').className = err ? 'err' : ''; }
async function importFiles(files) {
  const tot = { added: 0, updated: 0, kept: 0, gone: 0 }, skipped = [];
  for (const f of files) {
    try {
      const data = JSON.parse(await f.text());
      if (data.pack && data.pack !== DATA.pack) { skipped.push(`${f.name} (pack "${data.pack}")`); continue; }
      const n = merge(Array.isArray(data) ? data : data.decisions || []);
      for (const k in tot) tot[k] += n[k];
    } catch (e) { note(`${f.name}: not a decisions file (${e.message})`, true); return; }
  }
  save();
  if (skipped.length === files.length) { note(`Not imported, wrong pack: ${skipped.join(', ')}`, true); return; }
  note(`Imported: ${tot.added} new, ${tot.updated} updated, ${tot.kept} kept (yours were newer)` +
    (tot.gone ? `, ${tot.gone} for items no longer in this queue (kept, and exported again)` : '') +
    (skipped.length ? `. Skipped, wrong pack: ${skipped.join(', ')}` : ''));
}
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safe = (u) => /^https?:\/\//i.test(u || '') ? u : '';
for (const [k, v] of Object.entries(DATA.categories)) $('cat').append(new Option(v, k));
for (const r of DATA.reasons) $('bulkReason').append(new Option(r, r));
function visible() {
  const q = $('q').value.toLowerCase(), lo = +$('min').value, hi = +$('max').value, cat = $('cat').value, st = $('state').value;
  return DATA.items.filter((it) => it.confidence >= lo && it.confidence <= hi && (!cat || it.category === cat)
    && (!q || (it.title + ' ' + it.url + ' ' + it.summary + ' ' + it.description + ' ' + it.excerpt).toLowerCase().includes(q))
    && (!st || (st === 'undecided' ? !decided(it.id) : dec[it.id]?.action === st)));
}
function card(it) {
  const d = decided(it.id) ? dec[it.id] : null, probs = Object.entries(it.decisions || {}).map(([k, v]) =>
    `${k}=${typeof v === 'object' ? (v.choice ?? 'L' + v.level) : v}`).join(' · ');
  const reasons = DATA.reasons.map((r) => `<option ${d?.reason === r ? 'selected' : ''}>${r}</option>`).join('');
  return `<article class="${d?.action || ''}" data-id="${it.id}">
    <div class="row"><span class="conf">${it.confidence.toFixed(2)}</span>
      <a class="title" href="${esc(safe(it.url))}" target="_blank" rel="noopener">${esc(it.title || it.url)}</a>
      <span class="mono">${esc(it.url)}</span>
      <span class="actions"><button class="ok" data-a="approve">Approve</button>
      <select data-r>${reasons}</select><button class="no" data-a="reject">Reject</button>
      ${d ? '<button data-a="undo">Undo</button>' : ''}</span></div>
    <div class="row">${[DATA.categories[it.category] || it.category, it.tier ? 'tier ' + it.tier : '', ...it.sources, it.mode, it.ai_claim !== 'none' ? 'AI: ' + it.ai_claim : '']
      .filter(Boolean).map((c) => `<span class="chip">${esc(c)}</span>`).join('')}
      ${it.drift ? `<span class="chip warn">drift: ${esc(it.drift)}</span>` : ''}
      ${it.held ? `<span class="chip warn">${esc(it.held)}</span>` : ''}
      ${it.disagreements.map((x) => `<span class="chip warn">${esc(x)}</span>`).join('')}</div>
    ${it.summary ? `<div>${esc(it.summary)}</div>` : ''}${it.why ? `<div class="m">Why: ${esc(it.why)}</div>` : ''}
    ${it.description && it.description !== it.summary ? `<div class="m">Page says: ${esc(it.description)}</div>` : ''}
    ${probs ? `<div class="mono">${esc(probs)}${it.model ? ' — ' + esc(it.model) : ''}</div>` : ''}
    ${it.excerpt ? `<details><summary>Page text</summary>${esc(it.excerpt)}…</details>` : ''}
    <div class="mono">${it.found.map((u) => safe(u) ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>` : '').join(' · ')}</div>
  </article>`;
}
function draw() {
  const vis = visible();
  $('list').innerHTML = vis.slice(0, 300).map(card).join('') || '<p class="m">Nothing here.</p>';
  const here = DATA.items.filter((it) => decided(it.id)), a = here.filter((it) => dec[it.id].action === 'approve').length;
  $('stats').textContent = `${vis.length} shown · ${a} approve · ${here.length - a} reject · ${DATA.items.length - here.length} left`;
  $('prog').max = DATA.items.length || 1; $('prog').value = here.length;
  $('done').textContent = `${here.length} of ${DATA.items.length} decided` + (dirty ? ' · not exported yet' : '');
}
$('list').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-a]'); if (!b) return;
  const art = b.closest('article'), id = art.dataset.id, a = b.dataset.a;
  if (a === 'undo') set(id, 'clear'); else set(id, a, a === 'reject' ? art.querySelector('[data-r]').value : '');
  save();
});
$('bulkOk').onclick = () => { const v = visible(); if (confirm(`Approve ${v.length} items?`)) { v.forEach((it) => set(it.id, 'approve')); save(); } };
$('bulkNo').onclick = () => { const v = visible(), r = $('bulkReason').value; if (confirm(`Reject ${v.length} items as "${r}"?`)) { v.forEach((it) => set(it.id, 'reject', r)); save(); } };
$('dl').onclick = () => {
  const body = JSON.stringify({ pack: DATA.pack, generated: new Date().toISOString(),
    decisions: Object.entries(dec).map(([id, d]) => ({ id, ...d })) }, null, 1);
  const stamp = now().slice(0, 16).replace(/[T:]/g, '-');
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(new Blob([body], { type: 'application/json' })), download: `decisions-${DATA.pack}-${stamp}.json` });
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  dirty = false; draw(); note(`Exported ${Object.keys(dec).length} decisions. Import the file here next time, or apply it.`);
};
$('imp').onclick = () => $('file').click();
$('file').onchange = (e) => { importFiles([...e.target.files]); e.target.value = ''; };
addEventListener('dragover', (e) => { e.preventDefault(); document.body.classList.add('drop'); });
addEventListener('dragleave', (e) => { if (!e.relatedTarget) document.body.classList.remove('drop'); });
addEventListener('drop', (e) => { e.preventDefault(); document.body.classList.remove('drop'); importFiles([...e.dataTransfer.files]); });
addEventListener('beforeunload', (e) => { if (dirty) e.preventDefault(); });
// Earlier exports passed to `engine review-page --decisions` arrive embedded.
if (DATA.seed && DATA.seed.length) { const n = merge(DATA.seed); try { localStorage.setItem(KEY, JSON.stringify(dec)); } catch (e) {}
  note(`Loaded ${n.added + n.updated} earlier decisions from the files given to review-page.`); }
['q', 'min', 'max', 'cat', 'state'].forEach((id) => $(id).addEventListener('input', draw));
draw();
</script></body></html>
"""
