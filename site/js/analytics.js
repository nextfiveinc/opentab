// Analytics behind one track() function (plan: GoatCounter, no cookies, no personal
// data). Configured per pack in pack.yml `analytics: { provider: goatcounter, code }`.
// Everything here is best-effort: blocked script, offline, Do Not Track or no
// config all make track() a no-op, so analytics can never break the app. The
// service worker ignores cross-origin requests, so the script is never cached.

let ready = false;
let enabled = false;

export function initAnalytics(manifest) {
  const cfg = manifest.analytics || {};
  if (cfg.provider !== 'goatcounter' || !cfg.code) return;
  if (navigator.doNotTrack === '1' || window.doNotTrack === '1') return;
  enabled = true;
  const s = document.createElement('script');
  s.async = true;
  s.src = 'https://gc.zgo.at/count.js';
  s.dataset.goatcounter = `https://${encodeURIComponent(cfg.code)}.goatcounter.com/count`;
  // We count routes ourselves (hash routing), so the script must not auto-count.
  s.dataset.goatcounterSettings = JSON.stringify({ no_onload: true });
  s.onload = () => { ready = true; };
  document.head.append(s);
}

function send(payload) {
  if (!enabled || !ready || navigator.onLine === false) return;
  try { window.goatcounter?.count(payload); } catch { /* never let analytics throw */ }
}

/** Page view per route: '/', '/item/<id>', '/sources', '/digest', '/about'. */
export function pageview(path) {
  // The script may still be loading on the first route; retry once shortly after.
  if (enabled && !ready) { setTimeout(() => send({ path }), 1500); return; }
  send({ path });
}

/** Event named 'area/action'; title is an item or facet id, never free text. */
export function track(name, title = '') {
  send({ path: name, title, event: true });
}
