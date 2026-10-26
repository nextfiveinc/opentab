// Catalogue frontend: static site, no framework, no build step.
// All site text comes from manifest.copy; item data from catalogue.json.
// Public API (data contract): loadContract() resolves { manifest, items, cached }.

const CACHE_PREFIX = 'cat:';
const CONTRACT_KEY = 'cat:contract';
const CONTRACT_VERSION = 1; // highest data-contract version this frontend understands

/** JSON plus whether it came from an offline copy (the service worker marks
 *  responses it serves from its cache with 'x-served-from: cache'). */
async function fetchJson(url, ms = 8000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try {
    const res = await fetch(url, { signal: ctrl.signal, cache: 'no-cache' });
    if (!res.ok) throw new Error(`${url} -> ${res.status}`);
    return { data: await res.json(), offline: res.headers.get('x-served-from') === 'cache' };
  } finally {
    clearTimeout(t);
  }
}

function readCached() {
  try {
    const cached = localStorage.getItem(CONTRACT_KEY);
    return cached ? JSON.parse(cached) : null;
  } catch { return null; }
}

/**
 * Network first, localStorage as the offline fallback. (The old order — cache
 * first, network only when the cache was empty — served a visitor's first copy
 * of the catalogue forever.) `cached` is true whenever the data is an offline copy.
 */
async function loadContract() {
  try {
    const [m, c] = await Promise.all([fetchJson('./manifest.json'), fetchJson('./catalogue.json')]);
    const manifest = m.data;
    if ((manifest.contract_version || 1) > CONTRACT_VERSION) {
      // The plan's contract rule: refuse a newer contract rather than misrender it.
      throw new Error(`data contract v${manifest.contract_version} is newer than this site supports`);
    }
    const items = {};
    for (const it of c.data.items || []) items[it.id] = it;
    try {
      localStorage.setItem(CONTRACT_KEY, JSON.stringify({ manifest, items, fetched_at: Date.now() }));
    } catch { /* quota or storage disabled */ }
    return { manifest, items, cached: m.offline || c.offline };
  } catch (err) {
    const cached = readCached();
    if (cached?.manifest && cached?.items) {
      return { manifest: cached.manifest, items: cached.items, cached: true, fetched_at: cached.fetched_at };
    }
    throw err;
  }
}

/** Detail record (provenance, evidence, similar): network, then the offline copy, else null. */
async function loadItem(id) {
  const key = `${CACHE_PREFIX}item:${id}`;
  try {
    const { data } = await fetchJson(`./items/${encodeURIComponent(id)}.json`);
    try { localStorage.setItem(key, JSON.stringify(data)); } catch { /* quota */ }
    return data;
  } catch {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; }
  }
}

/** Any other published JSON file (sources, changelog); null when unavailable. */
async function loadJson(path) {
  try { return (await fetchJson(path)).data; } catch { return null; }
}

export { loadContract, loadItem, loadJson };
