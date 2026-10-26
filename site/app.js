// App bootstrap: loads the data contract, draws the frame (header, footer), registers
// routes and hands each one to its view module.

import { initAnalytics, pageview, track } from './js/analytics.js';
import { loadContract } from './js/data.js';
import { currentRoute, onRoute, register, routePath, start } from './js/router.js';
import { initTheme } from './js/theme.js';
import { el, icons } from './js/util.js';
import { renderDetail } from './js/views/detail.js';
import { renderGrid } from './js/views/grid.js';
import { onSavedChange, savedIds } from './js/local.js';
import { renderAbout, renderDigest, renderSaved, renderSource, renderSources } from './js/views/pages.js';

let manifest = null;
let items = {};
let fromCache = false;
let browse = null;          // the live browse view (for the '/' shortcut)
let lastBrowseHash = '#/';  // where "Back" returns to, filters included
const scrollByRoute = new Map();

const main = () => document.querySelector('main');
const copy = () => manifest.copy || {};
const siteTitle = () => copy().site_title || manifest.name || 'Catalogue';
// The Sources page is public only when the pack lists it in views; otherwise the
// maintainer keeps it locally (`engine sources-page`).
const publicSources = () => (manifest.views || []).includes('sources');

function renderHeader() {
  const c = copy();
  const views = manifest.views || ['grid'];
  const links = [
    ['#/', c.nav_browse || 'Browse'],
    views.includes('digest') ? ['#/digest', c.whats_new || "What's new"] : null,
    ['#/saved', c.saved_page || 'Saved'],
    publicSources() ? ['#/sources', c.sources_page || 'Sources'] : null,
    ['#/about', c.about_nav || c.about_page || 'About'],
  ].filter(Boolean);
  const themeBtn = (mode, icon, label) => el('button', {
    type: 'button', 'data-theme-choice': mode, 'aria-label': label, title: label, html: icon,
  });
  document.querySelector('header.site').replaceChildren(el('div', { class: 'header-inner' },
    el('a', { class: 'wordmark', href: '#/' }, el('span', { class: 'wordmark-mark', 'aria-hidden': 'true' }), siteTitle()),
    el('nav', { class: 'primary', 'aria-label': c.nav_label || 'Main' },
      links.map(([href, label]) => el('a', { href, 'data-route': href }, label,
        href === '#/saved' ? el('span', { class: 'count', 'data-saved-count': '' }) : null))),
    el('div', { class: 'theme-switch', role: 'group', 'aria-label': c.theme || 'Theme' },
      themeBtn('system', icons.system, c.theme_system || 'System'),
      themeBtn('light', icons.sun, c.theme_light || 'Light'),
      themeBtn('dark', icons.moon, c.theme_dark || 'Dark'))));
  initTheme(manifest, (mode) => track('theme-toggle', mode));
  const showCount = (ids) => document.querySelectorAll('[data-saved-count]').forEach((n) => {
    n.textContent = ids.length ? String(ids.length) : '';
  });
  showCount(savedIds());
  onSavedChange(showCount);
}

function renderFooter() {
  const c = copy();
  const lic = manifest.license || {};
  const outputs = manifest.outputs || [];
  document.querySelector('footer.site').replaceChildren(el('div', { class: 'footer-inner' },
    el('span', {}, siteTitle(), lic.data ? ` · ${c.footer_license || 'Data'} ${lic.data}` : '',
      lic.code ? ` · ${c.footer_code || 'Code'} ${lic.code}` : ''),
    el('nav', { 'aria-label': c.footer_nav || 'Footer' },
      outputs.includes('rss') ? el('a', { href: 'feeds/rss.xml', onclick: () => track('feed-click', 'rss') }, 'RSS') : null,
      outputs.includes('jsonfeed') ? el('a', { href: 'feeds/feed.json', onclick: () => track('feed-click', 'json') }, 'JSON Feed') : null,
      publicSources() ? el('a', { href: '#/sources' }, c.sources_page || 'Sources') : null,
      el('a', { href: '#/about' }, c.about_nav || c.about_page || 'About'))));
}

function offlineBanner() {
  return fromCache || navigator.onLine === false
    ? el('div', { class: 'banner', role: 'status' }, copy().offline_banner || 'Offline. Showing a saved copy.')
    : null;
}

/** Each route renders into a fresh <main>; stale async work checks isCurrent(). */
function page(title, render) {
  return (params) => {
    const token = currentRoute();
    const isCurrent = () => currentRoute() === token;
    document.title = title ? `${title} · ${siteTitle()}` : `${siteTitle()} · ${manifest.tagline || ''}`;
    const m = main();
    m.replaceChildren();
    const banner = offlineBanner();
    if (banner) m.append(banner);
    render(m, params, isCurrent);
  };
}

function boot(data) {
  manifest = data.manifest;
  items = data.items;
  fromCache = data.cached;
  renderHeader();
  renderFooter();
  initAnalytics(manifest);

  register('#/', page('', (m) => {
    browse = renderGrid(m, manifest, items);
  }));
  register('#/item/:id', page(null, (m, { id }, isCurrent) => {
    browse = null;
    renderDetail(m, manifest, items, id, isCurrent, lastBrowseHash);
  }));
  register('#/digest', page(copy().whats_new || "What's new", (m, _p, isCurrent) => {
    browse = null;
    renderDigest(m, manifest, items, isCurrent);
  }));
  if (publicSources()) {
    register('#/sources', page(copy().sources_page || 'Sources', (m, _p, isCurrent) => {
      browse = null;
      renderSources(m, manifest, isCurrent);
    }));
    register('#/source/:id', page(copy().sources_page || 'Sources', (m, { id }, isCurrent) => {
      browse = null;
      renderSource(m, manifest, items, id, isCurrent);
    }));
  }
  register('#/saved', page(copy().saved_page || 'Saved', (m) => {
    browse = null;
    renderSaved(m, manifest, items);
  }));
  register('#/about', page(copy().about_page || 'About', (m) => {
    browse = null;
    renderAbout(m, manifest);
  }));

  // Nav state, analytics, scroll: browse restores its position, others start at top.
  let prevPath = null;
  onRoute((pattern) => {
    const path = routePath();
    if (prevPath !== null) scrollByRoute.set(prevPath, window.scrollY);
    if (pattern === '#/') {
      lastBrowseHash = location.hash || '#/';
      requestAnimationFrame(() => window.scrollTo(0, scrollByRoute.get(path) || 0));
    } else if (prevPath !== null) {
      window.scrollTo(0, 0);
    }
    prevPath = path;
    document.querySelectorAll('nav.primary a').forEach((a) => {
      const active = a.dataset.route === `#${path}` || (a.dataset.route === '#/' && path.startsWith('/item/'))
        || (a.dataset.route === '#/sources' && path.startsWith('/source/'));
      if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    pageview(path);
  });
  window.addEventListener('scroll', () => {
    if (routePath() === '/') scrollByRoute.set('/', window.scrollY);
  }, { passive: true });

  // '/' focuses search from anywhere on the browse view.
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if (e.key === '/' && !typing && browse) { e.preventDefault(); browse.focusSearch(); }
  });

  start();
  if (fromCache) track('offline-served');
}

// The skip link can't use '#main' as a real anchor: that is a route change here.
document.querySelector('.skip-link')?.addEventListener('click', (e) => {
  e.preventDefault();
  main().focus();
});

// Only a failed load is reported as one; errors while rendering surface in the console.
loadContract().then(boot, (e) => {
  main().replaceChildren(el('div', { class: 'empty' }, `Could not load the catalogue: ${e.message}`));
});

// When a new worker takes over a page an older worker served, reload once so the
// page runs the freshly deployed code instead of what the old cache held.
if ('serviceWorker' in navigator) {
  const hadController = !!navigator.serviceWorker.controller;
  let reloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (hadController && !reloaded) { reloaded = true; location.reload(); }
  });
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}
