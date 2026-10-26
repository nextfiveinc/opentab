// Browse view: hero + search, quick filters, "three at random", then the facet
// sidebar and results grid. State (search, filters, sort) lives in the URL so any
// view can be shared; controls are built once and only the results re-render.

import { track } from '../analytics.js';
import {
  activeFilterCount, applyFacets, facetCounts, facetValues, queryFromState, sortItems,
  stateFromQuery, usableSorts, valueLabel,
} from '../facets.js';
import { formLink } from '../forms.js';
import { replaceQuery, routeQuery } from '../router.js';
import { isSaved, newSince, previousVisit, toggleSaved } from '../local.js';
import { search } from '../search.js';
import { daysAgo, domain, el, feedbackUrl, fill, hue, icons, safeUrl } from '../util.js';

const PAGE = 30;
let randomPick = null; // keeps "three at random" stable across re-renders until re-rolled

// ---- cards ------------------------------------------------------------------

function thumb(it, eager = false) {
  const placeholder = () => el('div', { class: 'thumb placeholder', style: `--hue:${hue(it.id)}` },
    el('span', { 'aria-hidden': 'true' }, (it.title || domain(it.url) || '?').trim().charAt(0)));
  const src = safeUrl(it.image?.src);
  if (!src) return placeholder();
  const img = el('img', {
    src, alt: it.image.alt || '', width: 640, height: 360,
    loading: eager ? 'eager' : 'lazy', decoding: 'async', referrerpolicy: 'no-referrer',
  });
  const box = el('div', { class: 'thumb' }, img);
  // Hotlinked previews disappear; fall back to the generated placeholder.
  img.addEventListener('error', () => box.replaceWith(placeholder()), { once: true });
  return box;
}

function badges(it, copy, max = 2) {
  const out = [];
  if (daysAgo(it.dates?.first_seen) < 7 && copy.new_marker) out.push(el('span', { class: 'tag new' }, copy.new_marker));
  // Build-computed markers from the changelog (content changed / revived).
  if ((it.markers || []).includes('back-online') && copy.back_online_marker) {
    out.push(el('span', { class: 'tag ok' }, copy.back_online_marker));
  }
  if ((it.markers || []).includes('updated') && copy.updated_marker) out.push(el('span', { class: 'tag accent' }, copy.updated_marker));
  if (it.ai_claim && it.ai_claim !== 'none') {
    const full = copy.ai_badges?.[it.ai_claim] || 'Built with AI';
    out.push(el('span', { class: 'tag accent', title: full }, copy.ai_short?.[it.ai_claim] || full));
  }
  const a = it.attrs || {};
  if (a.access === 'none' && copy.no_sign_in) out.push(el('span', { class: 'tag' }, copy.no_sign_in));
  if (a.access === 'required' && copy.sign_in_required) out.push(el('span', { class: 'tag' }, copy.sign_in_required));
  if (a.offline && copy.works_offline) out.push(el('span', { class: 'tag' }, copy.works_offline));
  if (a.open_source && copy.open_source) out.push(el('span', { class: 'tag' }, copy.open_source));
  if (a.cost === 'freemium' && copy.freemium) out.push(el('span', { class: 'tag' }, copy.freemium));
  return out.slice(0, max);
}

export function saveButton(it, copy, cls = 'save') {
  const label = () => (isSaved(it.id) ? (copy.unsave || 'Saved') : (copy.save || 'Save'));
  // The tooltip says what saving does: the list lives in this browser, not an account.
  const hint = () => (isSaved(it.id) ? (copy.unsave_hint || label()) : (copy.save_hint || label()));
  const btn = el('button', {
    class: cls, type: 'button', 'aria-pressed': String(isSaved(it.id)), title: hint(),
    'aria-label': `${label()}: ${it.title || domain(it.url)}`,
    onclick: (e) => {
      e.preventDefault();
      const on = toggleSaved(it.id);
      btn.setAttribute('aria-pressed', String(on));
      btn.title = hint();
      btn.setAttribute('aria-label', `${label()}: ${it.title || domain(it.url)}`);
      if (cls !== 'save') btn.lastChild.textContent = label();
      track('save-item', it.id);
    },
  }, el('span', { html: icons.star, style: 'width:16px;height:16px;display:inline-grid' }),
  cls === 'save' ? null : el('span', {}, label()));
  return btn;
}

/** "Know an app we're missing?" Sits under the results, so it is still there when a
 *  search finds nothing. Hidden when the pack has nowhere to send suggestions. */
export function suggestBox(manifest) {
  const copy = manifest.copy || {};
  const href = feedbackUrl(manifest, 'suggest_app');
  if (!href) return null;
  return el('aside', { class: 'suggest' },
    el('p', {}, el('strong', {}, copy.suggest_title || 'Know a free web app we should list?'), ' ',
      copy.suggest_body || 'Tell us about it.'),
    el('a', { class: 'btn', ...formLink(href, copy, () => track('submit-tip', 'app')) },
      copy.submit_tip || 'Suggest an app'));
}

export function renderItemCard(it, manifest, { eager = false } = {}) {
  const copy = manifest.copy || {};
  const href = safeUrl(it.url);
  return el('article', { class: 'card' },
    thumb(it, eager),
    el('div', { class: 'card-body' },
      el('h3', {}, el('a', { href: `#/item/${encodeURIComponent(it.id)}` }, it.title || domain(it.url))),
      el('div', { class: 'domain' }, domain(it.url)),
      it.summary ? el('p', { class: 'summary' }, it.summary) : null,
      el('div', { class: 'card-foot' },
        badges(it, copy),
        saveButton(it, copy),
        href ? el('a', {
          class: 'open', href, target: '_blank', rel: 'noopener',
          'aria-label': `${copy.open_app || 'Open app'}: ${it.title || domain(it.url)}`,
          onclick: () => track('open-app', it.id),
        }, `${copy.open_short || 'Open'} ↗`) : null)));
}

// ---- view ---------------------------------------------------------------------

export function renderGrid(container, manifest, items) {
  const copy = manifest.copy || {};
  const facets = manifest.facets || [];
  const all = Object.values(items).filter((it) => it.status === 'active');
  const sorts = usableSorts(manifest.sorts, items);
  const state = stateFromQuery(routeQuery(), facets);
  if (!sorts.includes(state.sort)) state.sort = sorts[0];
  state.sinceVisit = routeQuery().get('new') === '1';
  const since = previousVisit();
  const fresh = new Set(newSince(all, since).map((it) => it.id));
  let limit = PAGE;

  const sync = () => {
    const q = queryFromState(state, sorts[0]);
    if (state.sinceVisit) q.set('new', '1');
    replaceQuery(q);
  };
  const changed = () => { limit = PAGE; sync(); render(); };

  // Hero ------------------------------------------------------------------------
  const searchInput = el('input', {
    type: 'search', id: 'q', value: state.query, autocomplete: 'off',
    placeholder: copy.search_placeholder || 'Search…',
    'aria-label': copy.search_label || copy.search_placeholder || 'Search',
    oninput: (e) => { state.query = e.target.value; changed(); },
    onchange: () => { if (state.query) track('search-submit'); },
  });
  const hero = el('section', { class: 'hero' },
    el('p', { class: 'eyebrow' }, el('span', { class: 'dot', 'aria-hidden': 'true' }),
      fill(copy.hero_eyebrow || '{n} checked and working', { n: all.length })),
    el('h1', {}, copy.hero_title || manifest.tagline || manifest.name),
    copy.hero_lede ? el('p', { class: 'lede' }, copy.hero_lede) : null,
    el('div', { class: 'search', role: 'search' },
      el('span', { html: icons.search }), searchInput, el('kbd', { 'aria-hidden': 'true' }, '/')),
  );
  const quick = el('div', { class: 'quick', 'aria-label': copy.quick_filters || 'Quick filters' });
  hero.append(quick);
  if (fresh.size) {
    // "New since your last visit": browser-only, from the previous visit's time.
    hero.append(el('p', { class: 'since' },
      el('span', { class: 'tag new' }, fill(copy.new_since_visit || '{n} new since your last visit', { n: fresh.size })),
      ' ', el('button', {
        class: 'btn quiet', type: 'button',
        onclick: () => { state.sinceVisit = !state.sinceVisit; changed(); document.getElementById('browse')?.scrollIntoView(); },
      }, copy.show_them || 'Show them')));
  }

  // Three at random --------------------------------------------------------------
  const shelf = el('section', { class: 'shelf', 'aria-labelledby': 'shelf-title' });
  const drawShelf = () => {
    if (!randomPick) randomPick = sortItems(all, 'random').slice(0, 3).map((it) => it.id);
    shelf.replaceChildren(
      el('div', { class: 'shelf-head' },
        el('h2', { id: 'shelf-title' }, copy.three_random || 'Three at random'),
        el('button', {
          class: 'pill', type: 'button',
          onclick: () => { randomPick = null; drawShelf(); track('reroll-random'); },
        }, el('span', { html: icons.shuffle, style: 'width:15px;height:15px;display:inline-grid' }),
        copy.reroll || 'Re-roll')),
      el('div', { class: 'card-grid' }, randomPick.map((id) => items[id]).filter(Boolean)
        .map((it) => renderItemCard(it, manifest, { eager: true }))));
  };

  // Browse: facets + results ----------------------------------------------------------
  const facetsBox = el('details', { class: 'facets', 'aria-label': copy.filters || 'Filters' });
  if (!matchMedia('(max-width: 860px)').matches) facetsBox.open = true;
  const filterSummary = el('summary', {});
  const facetBody = el('div', {});
  facetsBox.append(filterSummary, facetBody);

  const count = el('p', { class: 'results-count', 'aria-live': 'polite' });
  const feedLink = el('span', { class: 'feed-link' });
  const chips = el('div', { class: 'active-filters' });
  const sortSelect = el('select', {
    id: 'sort',
    onchange: (e) => { state.sort = e.target.value; changed(); track('sort-change', state.sort); },
  }, sorts.map((s) => el('option', { value: s, selected: state.sort === s }, (copy.sort_labels || {})[s] || s)));
  const results = el('div', {});
  const browse = el('section', { class: 'browse', id: 'browse', 'aria-labelledby': 'browse-title' },
    el('div', { class: 'browse-head' }, el('h2', { id: 'browse-title' }, copy.browse_title || 'Browse everything')),
    facetsBox,
    el('div', {},
      el('div', { class: 'results-bar' }, count, feedLink,
        el('label', { class: 'sort', for: 'sort' }, copy.sort_by || 'Sort by', sortSelect), chips),
      results));

  const toggle = (facetId, value, on) => {
    const list = state.filters[facetId] || [];
    state.filters[facetId] = on ? [...new Set([...list, value])] : list.filter((v) => v !== value);
    if (!state.filters[facetId].length) delete state.filters[facetId];
    changed();
    track(on ? 'filter-apply' : 'filter-clear', facetId);
  };
  const clearAll = () => {
    state.filters = {}; state.query = ''; state.sinceVisit = false; searchInput.value = '';
    changed(); track('filter-clear', 'all');
  };

  function matching() {
    let list = state.query ? search(all, state.query) : all;
    if (state.sinceVisit) list = list.filter((it) => fresh.has(it.id));
    list = applyFacets(list, facets, state);
    // Search results keep relevance order unless the visitor picked a sort.
    return state.query && state.sort === sorts[0] ? list : sortItems(list, state.sort);
  }

  function drawFacets(pool) {
    facetBody.replaceChildren();
    const boolGroup = el('div', { class: 'facet-group' }, el('h3', {}, copy.facet_features || 'Features'));
    for (const facet of facets) {
      const counts = facetCounts(pool, facets, state, facet);
      const selected = state.filters[facet.id] || [];
      // Zero-count values are hidden (unless selected), so are empty groups.
      const values = facetValues(facet).filter((v) => counts[v] || selected.includes(v));
      if (!values.length) continue;
      const group = facet.type === 'bool' ? boolGroup
        : el('div', { class: 'facet-group' }, el('h3', {}, facet.label || facet.id));
      for (const v of values) {
        group.append(el('label', { class: 'facet-option' },
          el('input', { type: 'checkbox', checked: selected.includes(v), onchange: (e) => toggle(facet.id, v, e.target.checked) }),
          el('span', {}, valueLabel(facet, v, manifest)),
          el('span', { class: 'count' }, String(counts[v] || 0))));
      }
      if (facet.type !== 'bool') facetBody.append(group);
    }
    if (boolGroup.children.length > 1) facetBody.append(boolGroup);
    if (activeFilterCount(state)) {
      facetBody.append(el('button', { class: 'btn clear', type: 'button', onclick: clearAll }, copy.clear_filters || 'Clear all'));
    }
    const n = activeFilterCount(state);
    filterSummary.replaceChildren(
      el('span', {}, n ? `${copy.filters || 'Filters'} (${n})` : (copy.filters || 'Filters')),
      el('span', { html: icons.filter, style: 'width:18px;height:18px;display:inline-grid' }));
  }

  function drawQuick(pool) {
    quick.replaceChildren();
    for (const q of manifest.quick_filters || []) {
      const [facetId, value] = typeof q === 'string' ? [q, 'true'] : Object.entries(q)[0].map(String);
      const facet = facets.find((f) => f.id === facetId);
      if (!facet) continue;
      const on = (state.filters[facetId] || []).includes(value);
      const n = facetCounts(pool, facets, state, facet)[value] || 0;
      if (!n && !on) continue;
      const label = facet.type === 'bool' ? (facet.label || facetId) : valueLabel(facet, value, manifest);
      quick.append(el('button', {
        class: 'pill', type: 'button', 'aria-pressed': String(on),
        onclick: () => { toggle(facetId, value, !on); track('quick-filter', facetId); },
      }, on ? el('span', { html: icons.check, style: 'width:14px;height:14px;display:inline-grid' }) : null, label));
    }
  }

  function drawChips() {
    chips.replaceChildren();
    if (state.sinceVisit) {
      chips.append(el('button', {
        class: 'pill', type: 'button', onclick: () => { state.sinceVisit = false; changed(); },
      }, copy.since_visit_chip || 'New since your visit', el('span', { class: 'x', 'aria-hidden': 'true' }, '×')));
    }
    if (state.query) {
      chips.append(el('button', {
        class: 'pill', type: 'button',
        onclick: () => { state.query = ''; searchInput.value = ''; changed(); },
      }, `“${state.query}”`, el('span', { class: 'x', 'aria-hidden': 'true' }, '×')));
    }
    for (const [facetId, values] of Object.entries(state.filters)) {
      const facet = facets.find((f) => f.id === facetId);
      if (!facet) continue;
      for (const v of values) {
        const label = facet.type === 'bool' ? (facet.label || facetId) : valueLabel(facet, v, manifest);
        chips.append(el('button', {
          class: 'pill', type: 'button', 'aria-label': `${copy.remove_filter || 'Remove filter'}: ${label}`,
          onclick: () => toggle(facetId, v, false),
        }, label, el('span', { class: 'x', 'aria-hidden': 'true' }, '×')));
      }
    }
  }

  function render() {
    const pool = state.query ? search(all, state.query) : all;
    const list = matching();
    drawFacets(pool);
    drawQuick(pool);
    drawChips();
    shelf.hidden = Boolean(state.query || activeFilterCount(state) || state.sinceVisit) || all.length < 4;
    const noun = manifest.noun || { one: 'item', many: 'items' };
    count.textContent = `${list.length} ${list.length === 1 ? noun.one : noun.many}`;
    const cats = state.filters.category || [];
    feedLink.replaceChildren();
    if ((manifest.outputs || []).includes('rss') && cats.length === 1 && manifest.taxonomy?.categories?.[cats[0]]) {
      const label = manifest.taxonomy.categories[cats[0]].label || cats[0];
      feedLink.append(el('a', { href: `feeds/${encodeURIComponent(cats[0])}.xml`, onclick: () => track('feed-click', cats[0]) },
        fill(copy.follow_category || 'Follow {label} (RSS)', { label })));
    }

    if (!list.length) {
      results.replaceChildren(el('div', { class: 'empty' },
        el('p', {}, copy.no_results || 'Nothing found.'),
        el('button', { class: 'btn', type: 'button', onclick: clearAll }, copy.clear_filters || 'Clear all')));
      return;
    }
    const grid = el('div', { class: 'card-grid' }, list.slice(0, limit).map((it) => renderItemCard(it, manifest)));
    const rest = list.length - limit;
    const more = rest > 0 && el('div', { class: 'more' }, el('button', {
      class: 'btn', type: 'button',
      onclick: () => { limit += PAGE; render(); },
    }, fill(copy.show_more || 'Show {n} more', { n: Math.min(PAGE, rest) })));
    // replaceChildren(null) inserts the text "null"; pass only real nodes.
    results.replaceChildren(...[grid, more].filter(Boolean));
  }

  drawShelf();
  container.append(...[hero, shelf, browse, suggestBox(manifest)].filter(Boolean));
  render();
  return { focusSearch: () => searchInput.focus() };
}
