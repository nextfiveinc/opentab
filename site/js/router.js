// Tiny hash-based router. Routes: #/ (browse), #/item/<id>, #/digest, #/about, #/sources.
// Handlers receive the route params object, e.g. { id } for '#/item/:id'. The browse
// view keeps its search/filter/sort state in the hash's query string ('#/?q=…'),
// which is ignored for matching.

const routes = [];
const listeners = [];

export function register(pattern, handler) {
  // pattern like '#/item/:id' — strip the leading '#' and empty segments
  const parts = pattern.split('/').filter((p) => p && p !== '#');
  routes.push({ pattern, parts, handler });
}

/** Called after every route change with the matched pattern (analytics, nav state). */
export function onRoute(fn) { listeners.push(fn); }

export function navigate(hash) {
  if (location.hash === hash) handle();
  else location.hash = hash;
}

export function currentRoute() {
  return (location.hash || '#/').slice(1);
}

export function routePath() {
  return currentRoute().split('?')[0] || '/';
}

export function routeQuery() {
  return new URLSearchParams(currentRoute().split('?')[1] || '');
}

/** Rewrite the current route's query without a navigation or history entry. */
export function replaceQuery(params) {
  const qs = params.toString();
  history.replaceState(history.state, '', `#${routePath()}${qs ? `?${qs}` : ''}`);
}

function handle() {
  const segs = routePath().split('/').filter(Boolean);
  for (const r of routes) {
    if (r.parts.length !== segs.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < r.parts.length; i++) {
      if (r.parts[i].startsWith(':')) params[r.parts[i].slice(1)] = decodeURIComponent(segs[i]);
      else if (r.parts[i] !== segs[i]) { ok = false; break; }
    }
    if (ok) {
      r.handler(params);
      listeners.forEach((fn) => fn(r.pattern, params));
      return;
    }
  }
  // Unknown route: render home. Never fall back to an arbitrary view — the old
  // fallback picked the last-registered route and rendered Sources everywhere.
  const home = routes.find((r) => r.pattern === '#/') || routes.find((r) => !r.parts.length);
  if (home) {
    home.handler({});
    listeners.forEach((fn) => fn(home.pattern, {}));
  }
}

/**
 * Start routing once every route is registered: render the current URL (so deep
 * links survive a reload) and follow hash changes.
 */
export function start() {
  window.addEventListener('hashchange', handle);
  handle();
}
