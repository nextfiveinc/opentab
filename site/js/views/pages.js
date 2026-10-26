// Secondary pages: What's new (digest), Sources and About. All text from copy.yml.

import { track } from '../analytics.js';
import { loadJson } from '../data.js';
import { formLink } from '../forms.js';
import { savedIds } from '../local.js';
import { el, feedbackUrl, fill, fmtDate, safeUrl } from '../util.js';
import { renderItemCard, suggestBox } from './grid.js';

function head(title, intro) {
  return el('header', { class: 'page-head' }, el('h1', {}, title), intro ? el('p', {}, intro) : null);
}

function unavailable(copy) {
  return el('div', { class: 'empty' }, el('p', {}, copy.unavailable_offline || 'Not available offline yet.'));
}

// ---- digest: one entry per day from the published changelog -------------------

export async function renderDigest(main, manifest, items, isCurrent) {
  const copy = manifest.copy || {};
  main.replaceChildren(head(copy.whats_new || "What's new", copy.digest_intro), el('div', { class: 'loading' }, el('div'), el('div')));
  const index = await loadJson('./changelog/index.json');
  if (!isCurrent()) return;
  if (!index) { main.replaceChildren(head(copy.whats_new || "What's new"), unavailable(copy)); return; }
  const days = await Promise.all((index.days || []).slice(0, 30)
    .map((d) => loadJson(`./changelog/${d.date}.json`)));
  if (!isCurrent()) return;

  const list = days.filter(Boolean).map((day) => {
    const added = (day.added || []).map(({ id, title }) => {
      const it = items[id];
      return el('li', {},
        it ? el('a', { href: `#/item/${encodeURIComponent(id)}` }, it.title || title) : el('span', {}, title || id),
        it?.summary ? el('p', {}, it.summary) : null);
    });
    const removed = (day.removed || []).map(({ title, id }) => el('li', { class: 'removed' }, title || id));
    return el('article', { class: 'day' },
      el('time', { datetime: day.date }, fmtDate(day.date, { day: 'numeric', month: 'long' }),
        el('small', {}, fill(copy.digest_counts || '+{added} · −{removed}',
          { added: added.length, removed: removed.length }))),
      el('ul', { role: 'list' }, added, removed));
  });
  main.replaceChildren(...[head(copy.whats_new || "What's new", copy.digest_intro),
    list.length ? el('div', {}, list) : el('div', { class: 'empty' }, el('p', {}, copy.digest_empty || 'Nothing has changed yet.')),
    suggestBox(manifest)].filter(Boolean));
}

// ---- sources: where items come from, with health -------------------------------

export async function renderSources(main, manifest, isCurrent) {
  const copy = manifest.copy || {};
  const title = copy.sources_page || 'Sources';
  main.replaceChildren(head(title, copy.sources_intro), el('div', { class: 'loading' }, el('div')));
  const data = await loadJson('./sources.json');
  if (!isCurrent()) return;
  if (!data) { main.replaceChildren(head(title, copy.sources_intro), unavailable(copy)); return; }
  const rows = (data.sources || []).map((s) => el('tr', {},
    el('td', {}, el('a', { href: `#/source/${encodeURIComponent(s.id)}` }, el('strong', {}, s.id)),
      s.notes ? el('small', {}, s.notes) : null),
    el('td', {}, s.last_checked ? fmtDate(s.last_checked) : (copy.never_checked || 'Not yet')),
    el('td', { class: 'num' }, String(s.items_contributed ?? 0))));
  main.replaceChildren(head(title, copy.sources_intro),
    el('div', { class: 'table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, [copy.th_source || 'Source', copy.th_checked || 'Last checked', copy.th_items || 'Items']
        .map((h) => el('th', { scope: 'col' }, h)))),
      el('tbody', {}, rows))));
}

// ---- saved: this browser's saved items -------------------------------------------

export function renderSaved(main, manifest, items) {
  const copy = manifest.copy || {};
  const list = savedIds().map((id) => items[id]).filter(Boolean);
  main.append(head(copy.saved_page || 'Saved', copy.saved_intro),
    list.length
      ? el('div', { class: 'card-grid' }, list.map((it) => renderItemCard(it, manifest)))
      : el('div', { class: 'empty' }, el('p', {}, copy.saved_empty || 'Nothing saved yet.'),
        el('a', { class: 'btn', href: '#/' }, copy.nav_browse || 'Browse')));
}

// ---- one source: what it contributed ------------------------------------------------

export async function renderSource(main, manifest, items, id, isCurrent) {
  const copy = manifest.copy || {};
  const list = Object.values(items).filter((it) => (it.sources || []).includes(id));
  const data = await loadJson('./sources.json');
  if (!isCurrent()) return;
  const src = (data?.sources || []).find((s) => s.id === id);
  const facts = src?.last_checked ? `${copy.th_checked || 'Last checked'} ${fmtDate(src.last_checked)}` : '';
  main.append(
    el('a', { class: 'crumb', href: '#/sources' }, `← ${copy.sources_page || 'Sources'}`),
    head(id, [src?.notes, facts].filter(Boolean).join('. ')),
    list.length
      ? el('div', { class: 'card-grid' }, list.map((it) => renderItemCard(it, manifest)))
      : el('div', { class: 'empty' }, el('p', {}, copy.source_empty || 'Nothing listed from this source yet.')));
}

// ---- about: methodology, generated from the pack's copy and counts ------------------

export function renderAbout(main, manifest) {
  const copy = manifest.copy || {};
  const c = manifest.counts || {};
  const stat = (n, label) => el('div', { class: 'stat' }, el('b', {}, String(n ?? 0)), el('span', {}, label));
  const sections = (copy.about_sections || []).map((s) => [
    el('h2', {}, s.title), ...(s.body || []).map((p) => el('p', {}, p))]);
  // Feeds only when the pack publishes them (outputs: rss / jsonfeed).
  const outputs = manifest.outputs || [];
  const feeds = [['rss', 'feeds/rss.xml', 'RSS'], ['jsonfeed', 'feeds/feed.json', 'JSON Feed']]
    .filter(([kind]) => outputs.includes(kind)).map(([, href, label]) => el('a', { href: safeUrl(href) }, label));
  const cats = outputs.includes('rss')
    ? [...new Set(Object.values(manifest.facets?.find((f) => f.type === 'tree')?.values || []))] : [];
  const catFeeds = cats.map((c) => el('a', { href: safeUrl(`feeds/${encodeURIComponent(c)}.xml`) },
    manifest.taxonomy?.categories?.[c]?.label || c));
  const ask = [['suggest_app', copy.submit_tip || 'Suggest an app'], ['suggest_source', copy.suggest_source || 'Suggest a place to look']]
    .map(([kind, label]) => {
      const href = feedbackUrl(manifest, kind);
      return href && el('a', { class: 'btn', ...formLink(href, copy, () => track('submit-tip', kind)) }, label);
    }).filter(Boolean);
  main.replaceChildren(
    head(copy.about_page || 'About', manifest.tagline),
    el('div', { class: 'prose' },
      el('div', { class: 'stats' },
        stat(c.active, copy.stat_active || 'listed now'),
        stat(c.total, copy.stat_total || 'tracked in total'),
        stat(c.stale, copy.stat_stale || 'failing checks')),
      sections.flat(),
      el('h2', {}, copy.about_follow || 'Follow along'),
      copy.about_share ? el('p', {}, copy.about_share) : null,
      ask.length ? el('p', { class: 'actions' }, ask) : null,
      feeds.length ? el('p', {}, copy.about_feeds || 'Subscribe to new additions:', ' ',
        feeds.flatMap((a, i) => (i ? [' · ', a] : [a]))) : null,
      catFeeds.length ? el('p', {}, copy.about_category_feeds || 'Or one category (RSS):', ' ',
        catFeeds.flatMap((a, i) => (i ? [' · ', a] : [a]))) : null,
      el('p', { class: 'domain' }, fill(copy.data_version || 'Data version {v}, built {date}', {
        v: manifest.version_hash || '', date: fmtDate(manifest.generated_at),
      }))));
}
