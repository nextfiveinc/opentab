// Service worker: network-first for everything, cache only as the offline fallback.
// The build rewrites VERSION to 'v4-<build hash>' so every rebuild gets a fresh cache.
//
// Why not cache-first for the app shell: a cache-first shell pins whatever code
// the browser saw first. That is what kept the fixed router.js from ever reaching
// browsers that had visited before — the page kept rendering the Sources view on
// '#/'. Network-first costs one conditional request (usually a 304) per asset and
// guarantees an online visitor always runs the deployed code.
const VERSION = 'v4';
const SHELL = [
  './', './index.html',
  './app.js', './js/data.js', './js/facets.js', './js/search.js', './js/router.js',
  './js/theme.js', './js/url_utils.js', './js/util.js', './js/analytics.js', './js/local.js', './js/forms.js',
  './js/views/grid.js', './js/views/detail.js', './js/views/pages.js',
  './css/app.css', './default-theme/tokens.css', './theme.css',
  './manifest.json', './catalogue.json', './changelog/index.json',
];

self.addEventListener('install', (e) => {
  // cache: 'reload' bypasses the HTTP cache — otherwise the precache can be filled
  // from heuristically-cached stale copies (static servers rarely send Cache-Control).
  e.waitUntil(
    caches.open(VERSION)
      .then((c) => Promise.all(SHELL.map((u) =>
        fetch(new Request(u, { cache: 'reload' }))
          .then((res) => (res.ok ? c.put(u, res) : null))
          .catch(() => null))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))),
    ).then(() => self.clients.claim()),
  );
});

// Responses served from the cache carry a header, so the page can say it is offline
// even when the browser thinks it is online (e.g. the server is down).
function markOffline(res) {
  const headers = new Headers(res.headers);
  headers.set('x-served-from', 'cache');
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin) return;

  // Everything — the page itself included — is revalidated against the server
  // instead of the HTTP cache. A heuristically cached index.html points at the
  // previous build's versioned app.js, which drags in that build's whole module set.
  // (A 'navigate' request can't be re-created, so fetch its URL as a plain GET.)
  const req = new Request(e.request.url, { cache: 'no-cache', credentials: 'same-origin' });

  e.respondWith(
    fetch(req)
      // A navigation may not be answered with a followed redirect; re-issue it.
      .then((res) => (e.request.mode === 'navigate' && res.redirected ? Response.redirect(res.url, 302) : res))
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true })
        .then((hit) => hit || (e.request.mode === 'navigate' ? caches.match('./index.html') : undefined))
        .then((hit) => (hit ? markOffline(hit) : Response.error()))),
  );
});
