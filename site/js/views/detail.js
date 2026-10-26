// Detail view (#/item/<id>): everything a visitor needs to decide whether to open
// the app, plus where it was found and why it is listed. The catalogue entry
// renders instantly; the detail file (provenance, evidence, similar) fills in.

import { track } from '../analytics.js';
import { loadItem } from '../data.js';
import { formLink } from '../forms.js';
import { valueLabel } from '../facets.js';
import { domain, el, feedbackUrl, fill, fmtDate, hue, icons, safeUrl } from '../util.js';
import { renderItemCard, saveButton } from './grid.js';

function link(u, text) {
  const href = safeUrl(u);
  return href ? el('a', { href, target: '_blank', rel: 'noopener' }, text) : el('span', {}, text);
}

function facts(it, manifest) {
  const copy = manifest.copy || {};
  const facets = Object.fromEntries((manifest.facets || []).map((f) => [f.id, f]));
  const label = (id, v) => (facets[id] ? valueLabel(facets[id], String(v), manifest) : String(v));
  const a = it.attrs || {};
  const cat = manifest.taxonomy?.categories?.[it.category];
  const sub = (cat?.subcategories || []).find((s) => (s.id || s) === it.subcategory);
  const yes = copy.facet_true || 'Yes';
  const no = copy.facet_false || 'No';
  const rows = [
    [copy.fact_category || 'Category', [cat?.label || it.category, sub?.label].filter(Boolean).join(' › ')],
    [copy.fact_cost || 'Cost', a.cost && label('cost', a.cost)],
    [copy.fact_access || 'Sign-in', a.access && label('access', a.access)],
    [copy.fact_device || 'Works on', (a.device || []).map((d) => label('device', d)).join(', ')],
    [copy.works_offline || 'Works offline', a.offline == null ? '' : (a.offline ? yes : no)],
    [copy.open_source || 'Open source', a.open_source == null ? '' : (a.open_source ? yes : no)],
    [copy.built_with || 'Built with', (a.built_with || []).map((b) => label('built_with', b)).join(', ')],
    [copy.fact_added || 'Added', fmtDate(it.dates?.first_seen)],
  ].filter(([, v]) => v);
  return el('dl', { class: 'facts' }, rows.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)]));
}

// `item` is the bare id (letters and digits, safe in any form's URL parameters);
// the app's address travels as `url`.
function reportUrl(manifest, it) {
  return feedbackUrl(manifest, 'report', { item: it.id, url: it.url },
    { title: `Problem with ${it.title || it.id} (${it.id})` });
}

/** One "Found on" line. With a public Sources page the source links there; without
 *  one, the line names the site it was found on and links to that page itself. */
function provenanceLine(p, it, manifest) {
  const copy = manifest.copy || {};
  const when = p.seen ? `, ${fmtDate(p.seen)}` : '';
  const elsewhere = p.url && p.url !== it.url;
  if ((manifest.views || []).includes('sources')) {
    return el('li', {}, el('a', { href: `#/source/${encodeURIComponent(p.source)}` }, el('strong', {}, p.source)), when,
      elsewhere ? [': ', link(p.url, domain(p.url) || p.url)] : '');
  }
  return el('li', {}, elsewhere ? link(p.url, domain(p.url) || p.url) : (copy.found_by_hand || 'Added by hand'), when);
}

function body(it, manifest, items, backHref) {
  const copy = manifest.copy || {};
  const title = it.title || domain(it.url);
  const appHref = safeUrl(it.url);
  const src = safeUrl(it.image?.src);
  const media = src
    ? el('img', { src, alt: it.image.alt || '', width: 1200, height: 675, referrerpolicy: 'no-referrer' })
    : el('span', { 'aria-hidden': 'true' }, title.charAt(0));
  const thumb = el('div', { class: src ? 'thumb' : 'thumb placeholder', style: src ? null : `--hue:${hue(it.id)}` }, media);
  if (src) {
    media.addEventListener('error', () => {
      thumb.className = 'thumb placeholder';
      thumb.style.setProperty('--hue', hue(it.id));
      thumb.replaceChildren(el('span', { 'aria-hidden': 'true' }, title.charAt(0)));
    }, { once: true });
  }

  const share = async () => {
    const url = location.href;
    track('share-link', it.id);
    try {
      if (navigator.share) await navigator.share({ title, url });
      else { await navigator.clipboard.writeText(url); shareBtn.textContent = copy.link_copied || 'Link copied'; }
    } catch { /* dismissed */ }
  };
  const shareBtn = el('button', { class: 'btn', type: 'button', onclick: share }, copy.share || 'Share');
  const report = reportUrl(manifest, it);

  const prov = (it.provenance || []).map((p) => provenanceLine(p, it, manifest));
  const ev = (it.evidence || []).map((e) => el('li', {},
    `${e.kind || 'mention'}: `, link(e.url, e.url)));
  const reportLine = report ? el('p', { class: 'report-line' }, copy.report_lead || 'Something wrong with this listing?', ' ',
    el('a', formLink(report, copy, () => track('report-problem', it.id)), copy.report_problem || 'Report it')) : null;
  const similar = (it.similar || []).map((id) => items[id]).filter(Boolean);

  return [
    // Back returns to the browse view as it was left (search, filters, sort).
    el('a', { class: 'crumb', href: backHref || '#/' }, `← ${copy.back || 'Back'}`),
    el('div', { class: 'detail' },
      thumb,
      el('div', {},
        el('h1', {}, title),
        el('p', { class: 'domain' }, domain(it.url)),
        it.summary ? el('p', { class: 'summary' }, it.summary) : null,
        el('div', { class: 'actions' },
          appHref ? el('a', {
            class: 'btn primary', href: appHref, target: '_blank', rel: 'noopener',
            onclick: () => track('open-app', it.id),
          }, copy.open_app || 'Open app', el('span', { html: icons.external, style: 'width:16px;height:16px;display:inline-grid' })) : null,
          saveButton(it, copy, 'btn'),
          shareBtn),
        it.dates?.last_verified
          ? el('p', { class: 'verified' }, el('span', { html: icons.check, style: 'width:16px;height:16px;display:inline-grid' }),
            fill(copy.verified_badge || 'Opened and working on {date}', { date: fmtDate(it.dates.last_verified) }))
          : null,
        it.why ? el('div', { class: 'why' }, el('strong', {}, copy.detail_why || 'Why it is listed'), it.why) : null,
        facts(it, manifest),
        el('section', { class: 'detail-section' },
          prov.length ? [el('h2', {}, copy.detail_provenance || 'Found on'), el('ul', {}, prov)] : null,
          reportLine),
        ev.length ? el('section', { class: 'detail-section' },
          el('h2', {}, copy.detail_evidence || 'Evidence'), el('ul', {}, ev)) : null)),
    similar.length ? el('section', { class: 'similar' },
      el('h2', {}, copy.similar || 'Similar'),
      el('div', { class: 'card-grid' }, similar.map((s) => renderItemCard(s, manifest)))) : null,
  ];
}

export function renderDetail(main, manifest, items, id, isCurrent, backHref) {
  const it = items[id];
  if (!it) {
    main.replaceChildren(el('div', { class: 'empty' },
      el('p', {}, manifest.copy?.not_found || 'That listing is not in the catalogue (any more).'),
      el('a', { class: 'btn', href: '#/' }, manifest.copy?.back || 'Back')));
    return;
  }
  document.title = `${it.title} · ${manifest.copy?.site_title || manifest.name}`;
  main.replaceChildren(...body(it, manifest, items, backHref).filter(Boolean));
  loadItem(id).then((detail) => {
    if (!detail || !isCurrent()) return; // offline without a copy, or navigated away
    main.replaceChildren(...body({ ...it, ...detail }, manifest, items, backHref).filter(Boolean));
  });
}
