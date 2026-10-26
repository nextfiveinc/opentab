// Report and suggest links (pack.yml `links`). A Tally form opens as a popup over the
// page, through Tally's own embed script; its URL parameters become the form's hidden
// fields. The script loads only when a visitor points at or focuses one of these
// links, so nobody else sends anything to Tally. Any other form, or a click before the
// script is ready, opens in a new tab (the link's own target): the site is never
// replaced.

import { el } from './util.js';

const TALLY_FORM = /^https:\/\/tally\.so\/r\/([A-Za-z0-9]+)/;
const TALLY_SCRIPT = 'https://tally.so/widgets/embed.js';
let loading = null;

function loadTally() {
  if (window.Tally) return Promise.resolve(window.Tally);
  loading ||= new Promise((resolve, reject) => {
    const s = el('script', { src: TALLY_SCRIPT, async: true });
    s.onload = () => (window.Tally ? resolve(window.Tally) : reject(new Error('no Tally')));
    s.onerror = () => { loading = null; reject(new Error('Tally script blocked')); };
    document.head.append(s);
  });
  return loading;
}

function toast(text) {
  if (!text) return;
  const t = el('div', { class: 'toast', role: 'status' }, text);
  document.body.append(t);
  setTimeout(() => t.remove(), 4000);
}

/** Attributes for a form link: `el('a', formLink(href, copy, onOpen), label)`. */
export function formLink(href, copy, onOpen) {
  const tally = TALLY_FORM.exec(href);
  const warm = tally ? () => { loadTally().catch(() => {}); } : null;
  return {
    href, target: '_blank', rel: 'noopener',
    onpointerenter: warm, onfocus: warm, ontouchstart: warm,
    onclick: (e) => {
      onOpen?.();
      if (!tally || !window.Tally) return; // new tab
      e.preventDefault();
      window.Tally.openPopup(tally[1], {
        layout: 'modal', width: 560, autoClose: 1500,
        hiddenFields: Object.fromEntries(new URL(href).searchParams),
        onSubmit: () => toast(copy.form_thanks || 'Thanks. We\'ll take a look.'),
      });
    },
  };
}
