// Facet filtering, counts, labels and sorting. Pure functions over the item list,
// driven by manifest.facets. Values within a facet combine with OR, facets with AND.

const RECENCY_DAYS = { '7d': 7, '30d': 30, '90d': 90 };

export function activeFilterCount(state) {
  let n = 0;
  for (const v of Object.values(state.filters)) n += Array.isArray(v) ? v.length : (v ? 1 : 0);
  return n;
}

function attr(item, field) {
  return (item.attrs || {})[String(field).replace(/^attrs\./, '')];
}

export function itemMatchesFacet(item, facet, wanted) {
  if (!wanted || !wanted.length) return true;
  switch (facet.type) {
    case 'tree': {
      const cat = item[facet.field[0]] || '';
      const sub = item[facet.field[1]] || '';
      // A category filter matches the category or any of its subcategories.
      return wanted.some((w) => w === cat || w === `${cat}/${sub}` || (sub && w === sub));
    }
    case 'enum':
      return wanted.includes(String(attr(item, facet.field) ?? ''));
    case 'multi': {
      const value = attr(item, facet.field) || [];
      return wanted.some((w) => value.includes(w));
    }
    case 'bool':
      return wanted.map(String).includes(String(Boolean(attr(item, facet.field))));
    case 'recency': {
      // Date fields live under item.dates (e.g. "dates.first_seen").
      const key = facet.field.replace(/^dates\./, '');
      const seen = (item.dates || {})[key] || item[key] || null;
      if (!seen) return false;
      const days = (Date.now() - new Date(seen).getTime()) / 86400000;
      return wanted.some((w) => days <= (RECENCY_DAYS[w] || 3650));
    }
    default:
      return true;
  }
}

/** Active items matching every facet filter except `skip` (used for counts). */
export function applyFacets(items, facets, state, skip = null) {
  return items.filter((it) => {
    if (it.status !== 'active') return false;
    for (const facet of facets) {
      if (facet.id === skip) continue;
      const wanted = state.filters[facet.id];
      if (wanted && !itemMatchesFacet(it, facet, wanted)) return false;
    }
    return true;
  });
}

/** Selectable values of a facet: manifest values, recency presets, or 'true' for bools. */
export function facetValues(facet) {
  if (facet.type === 'recency') return (facet.presets || facet.values || []).map(String);
  if (facet.type === 'bool') return ['true'];
  return (facet.values || []).map(String);
}

/** Count per value given every *other* active filter, so counts react to them. */
export function facetCounts(items, facets, state, facet) {
  const pool = applyFacets(items, facets, state, facet.id);
  const counts = {};
  for (const v of facetValues(facet)) {
    counts[v] = pool.filter((it) => itemMatchesFacet(it, facet, [v])).length;
  }
  return counts;
}

/** Human label for a facet value: taxonomy labels, copy.facet_values, presets. */
export function valueLabel(facet, value, manifest) {
  const copy = manifest.copy || {};
  if (facet.type === 'tree') {
    return manifest.taxonomy?.categories?.[value]?.label || prettify(value);
  }
  if (facet.type === 'recency') return (copy.recency_presets || {})[value] || value;
  if (facet.type === 'bool') return facet.label || facet.id;
  return (copy.facet_values?.[facet.id] || {})[value] || prettify(value);
}

function prettify(s) {
  const t = String(s).replace(/[-_]/g, ' ');
  return t.charAt(0).toUpperCase() + t.slice(1);
}

// ---- URL state: '#/?q=sql&sort=az&cost=free,freemium' -----------------------

export function stateFromQuery(params, facets) {
  const filters = {};
  for (const f of facets) {
    const raw = params.get(f.id);
    if (raw) filters[f.id] = raw.split(',').filter(Boolean);
  }
  return { query: params.get('q') || '', sort: params.get('sort') || '', filters };
}

export function queryFromState(state, defaultSort) {
  const p = new URLSearchParams();
  if (state.query) p.set('q', state.query);
  if (state.sort && state.sort !== defaultSort) p.set('sort', state.sort);
  for (const [k, v] of Object.entries(state.filters)) if (v.length) p.set(k, v.join(','));
  return p;
}

// ---- sorting -----------------------------------------------------------------

export function sortItems(items, sortKey) {
  const arr = [...items];
  const newest = (a, b) => (b.dates?.first_seen || '').localeCompare(a.dates?.first_seen || '');
  switch (sortKey) {
    case 'updated':
      arr.sort((a, b) => (b.dates?.last_verified || '').localeCompare(a.dates?.last_verified || '') || newest(a, b));
      break;
    case 'picks':
      arr.sort((a, b) => Number(Boolean(b.quality?.pick)) - Number(Boolean(a.quality?.pick)) || newest(a, b));
      break;
    case 'mentions':
      arr.sort((a, b) => (b.quality?.mentions || 1) - (a.quality?.mentions || 1) || newest(a, b));
      break;
    case 'az':
      arr.sort((a, b) => (a.title || '').localeCompare(b.title || ''));
      break;
    case 'random':
      for (let i = arr.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [arr[i], arr[j]] = [arr[j], arr[i]];
      }
      break;
    default: // newest
      arr.sort(newest);
  }
  return arr;
}

/** Sorts that need data the catalogue doesn't have yet would silently do nothing. */
export function usableSorts(sorts, items) {
  const all = Object.values(items);
  return (sorts || ['newest']).filter((s) => {
    if (s === 'picks') return all.some((it) => it.quality?.pick);
    if (s === 'mentions') return all.some((it) => (it.quality?.mentions || 1) > 1);
    return true;
  });
}
