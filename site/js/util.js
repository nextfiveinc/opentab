// Small DOM and formatting helpers shared by every view.

/** el('a', { class: 'x', href, onclick }, ...children). null/undefined/false attrs are skipped. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    // setAttribute('selected', undefined) would *set* it — skip absent values.
    if (v == null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v; // trusted, static markup only (icons)
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false || c === '') continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/** Only http(s) URLs become links: item, evidence and provenance URLs come from
 *  scraped pages and LLM output, so a 'javascript:' URL must never reach an href. */
export function safeUrl(u) {
  if (!u || typeof u !== 'string') return ''; // new URL(undefined, base) is a valid URL
  try {
    const url = new URL(u, location.href);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : '';
  } catch { return ''; }
}

export function domain(u) {
  try { return new URL(u).hostname.replace(/^www\./, ''); } catch { return ''; }
}

/** Stable hue per item, for image placeholders. */
export function hue(id) {
  let h = 0;
  for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

export function fmtDate(iso, opts = { day: 'numeric', month: 'short', year: 'numeric' }) {
  if (!iso) return '';
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, opts);
}

export function daysAgo(iso) {
  if (!iso) return Infinity;
  return (Date.now() - new Date(iso).getTime()) / 86400000;
}

/** "{n} apps" style templates from copy.yml. */
export function fill(template, vars) {
  return String(template || '').replace(/\{(\w+)\}/g, (_, k) => (vars[k] ?? ''));
}

export function storage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* private mode or storage disabled */ }
  return null;
}

// Inline icons (static markup, currentColor).
const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
export const icons = {
  search: svg('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>'),
  system: svg('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'),
  sun: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'),
  moon: svg('<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>'),
  external: svg('<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
  shuffle: svg('<path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/>'),
  check: svg('<path d="m5 12 5 5L20 7"/>'),
  filter: svg('<path d="M4 5h16M7 12h10M10 19h4"/>'),
  star: svg('<path d="m12 3.5 2.6 5.3 5.9.9-4.2 4.1 1 5.8L12 16.9l-5.3 2.7 1-5.8-4.2-4.1 5.9-.9z"/>'),
};

/** Where visitors report a problem or suggest something (pack.yml `links`). A direct
 *  URL for the kind (links.report, links.suggest_app, links.suggest_source; a Tally or
 *  Google form, say) wins. Otherwise links.issues points at the repo's issues, and this
 *  opens the matching issue form (.github/ISSUE_TEMPLATE/<kind>.yml). Either way
 *  `fields` go along as URL parameters, which pre-fill the form's fields (Tally hidden
 *  fields) of the same name. `title` is GitHub's issue title, so only the issue form
 *  gets it. '' when nothing is set: the caller then shows no link. */
export function feedbackUrl(manifest, kind, fields = {}, { title = '' } = {}) {
  const links = manifest.links || {};
  const direct = safeUrl(links[kind]);
  const base = safeUrl(links.issues);
  if (!direct && !base) return '';
  const u = new URL(direct || `${base.replace(/\/$/, '')}/new`);
  if (!direct) {
    u.searchParams.set('template', `${kind.replace(/_/g, '-')}.yml`);
    if (title) u.searchParams.set('title', title);
  }
  for (const [k, v] of Object.entries(fields)) if (v) u.searchParams.set(k, v);
  return u.href;
}
