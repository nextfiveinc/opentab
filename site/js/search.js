// Client-side search: builds an index in-memory from the item list (titles, summaries,
// tags, category). Small corpora (≤ ~20k) make this fast enough without a worker.

import { titleKey } from './url_utils.js';

let index = null;

function buildIndex(items) {
  const docs = items.map((it) => ({
    id: it.id,
    text: [
      it.title, it.summary, it.why,
      (it.tags || []).join(' '),
      it.category, it.subcategory,
    ].join(' ').toLowerCase(),
    title: titleKey(it.title),
  }));
  return docs;
}

export function search(items, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return items;
  if (!index || index.items !== items) {
    index = { items, docs: buildIndex(items) };
  }
  const terms = q.split(/\s+/).filter(Boolean);
  const scored = [];
  for (const doc of index.docs) {
    let score = 0;
    let all = true;
    for (const t of terms) {
      const hit = doc.text.includes(t);
      if (!hit) { all = false; break; }
      score += doc.title.includes(t) ? 3 : 1;
    }
    if (all && score > 0) scored.push({ id: doc.id, score });
  }
  scored.sort((a, b) => b.score - a.score);
  const byId = new Map(items.map((it) => [it.id, it]));
  return scored.map((s) => byId.get(s.id)).filter(Boolean);
}
