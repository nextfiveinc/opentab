// Browser-only extras (plan: no accounts): saved items and "new since your last
// visit". Everything lives in this browser's storage and degrades to nothing when
// storage is unavailable.

import { storage } from './util.js';

const SAVED = 'cat:saved';
const LAST_VISIT = 'cat:last_visit';
const VISIT_START = 'cat:visit_prev';
const listeners = new Set();

export function savedIds() {
  try { return JSON.parse(storage(SAVED) || '[]'); } catch { return []; }
}

export function isSaved(id) { return savedIds().includes(id); }

export function toggleSaved(id) {
  const ids = savedIds();
  const next = ids.includes(id) ? ids.filter((x) => x !== id) : [id, ...ids];
  storage(SAVED, JSON.stringify(next));
  listeners.forEach((fn) => fn(next));
  return next.includes(id);
}

export function onSavedChange(fn) { listeners.add(fn); }

/**
 * The previous visit's timestamp (ms), fixed for this whole session so the
 * "new since" set doesn't shrink as you browse; null on a first visit.
 */
export function previousVisit() {
  let prev = null;
  try { prev = sessionStorage.getItem(VISIT_START); } catch { /* ignore */ }
  if (prev === null) {
    prev = storage(LAST_VISIT) || '';
    try { sessionStorage.setItem(VISIT_START, prev); } catch { /* ignore */ }
    storage(LAST_VISIT, String(Date.now()));
  }
  return prev ? Number(prev) : null;
}

export function newSince(items, since) {
  if (!since) return [];
  return items.filter((it) => it.dates?.first_seen && new Date(`${it.dates.first_seen}T00:00:00`).getTime() > since);
}
