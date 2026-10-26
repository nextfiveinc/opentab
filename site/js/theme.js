// Theme handling: color mode (system/light/dark) via the data-theme attribute, persisted.

import { storage } from './util.js';

const KEY = 'cat:theme';
const media = matchMedia('(prefers-color-scheme: dark)');
let mode = 'system';

function apply() {
  const effective = mode === 'system' ? (media.matches ? 'dark' : 'light') : mode;
  document.documentElement.dataset.theme = effective;
  document.querySelectorAll('[data-theme-choice]').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(btn.dataset.themeChoice === mode));
  });
}

// One listener for the page's lifetime (it used to add another on every click).
media.addEventListener('change', () => { if (mode === 'system') apply(); });

export function initTheme(manifest, onChange = () => {}) {
  mode = storage(KEY) || manifest.theme?.default_color_mode || 'system';
  apply();
  document.querySelectorAll('[data-theme-choice]').forEach((btn) => {
    btn.addEventListener('click', () => { setTheme(btn.dataset.themeChoice); onChange(mode); });
  });
}

export function setTheme(next) {
  mode = next;
  storage(KEY, next);
  apply();
}
