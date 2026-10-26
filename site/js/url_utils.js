// Small shared utilities: URL normalisation and title keys for dedupe/search.

export function normaliseUrl(u) {
  try {
    const url = new URL(u);
    url.hash = '';
    const drop = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'ref'];
    drop.forEach((p) => url.searchParams.delete(p));
    let s = url.toString();
    if (s.endsWith('/') && new URL(s).pathname !== '/') s = s.slice(0, -1);
    return s;
  } catch { return u; }
}

export function titleKey(t) {
  return (t || '').toLowerCase().replace(/[^a-z0-9]+/g, '').slice(0, 40);
}
