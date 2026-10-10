/** Shared reader controls. Adapters own compilation, source selection and layout;
 * navigation, search and outline use the same PDF core and accessible controls.
 */
import '../../styles/application/24-paper-preview-shell.css';
import '../../styles/application/24-paper-preview-controls.css';
import '../../styles/application/24-paper-preview-responsive.css';

import type { Translator } from '../../i18n';
import type { PdfReader, ReaderState } from './pdf-reader';

export const readerIcons = {
  close: 'M6 6l12 12M18 6L6 18', download: 'M12 3v12m-5-5 5 5 5-5M5 16v5h14v-5',
  search: 'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  more: 'M4 6h16M4 12h16M4 18h16', refresh: 'M20 7V3l-4 4M20 7A9 9 0 1 0 21 14',
  reading: 'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5',
  fit: 'M8 4H4v16h4m8-16h4v16h-4M4 12h16m-7-3 3 3-3 3',
};
export function readerIcon(name: keyof typeof readerIcons): string {
  return `<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${readerIcons[name]}"/></svg>`;
}
export function readerButton(label: string, text: string, action: () => void,
  signal: AbortSignal, glyph?: keyof typeof readerIcons): HTMLButtonElement {
  const button = document.createElement('button'); button.type = 'button'; button.className = 'paper-preview-button';
  button.title = label; button.setAttribute('aria-label', label);
  if (glyph) button.innerHTML = readerIcon(glyph); else button.textContent = text;
  button.onclick = () => { if (!signal.aborted) action(); };
  return button;
}

export class ReaderNavigation {
  readonly element = document.createElement('div');
  readonly page = document.createElement('input');
  readonly zoom = document.createElement('input');
  private readonly count = document.createElement('span');

  constructor(reader: () => PdfReader, tr: Translator, signal: AbortSignal) {
    this.element.className = 'paper-preview-navigation';
    this.page.type = 'number'; this.page.min = '1'; this.page.value = '1';
    this.page.className = 'paper-preview-page-number'; this.page.setAttribute('aria-label', tr('preview.page'));
    this.page.addEventListener('change', () => reader().goToPage(Number(this.page.value)), { signal });
    this.zoom.inputMode = 'decimal'; this.zoom.className = 'paper-preview-zoom';
    this.zoom.setAttribute('aria-label', tr('preview.zoom'));
    this.zoom.addEventListener('change', () => reader().setScale(Number.parseFloat(this.zoom.value) / 100), { signal });
    this.count.className = 'paper-preview-page-count';
    const fit = readerButton(tr('paper.fitWidth'), tr('preview.fit'), () => reader().setScale('fit'), signal, 'fit');
    fit.classList.add('paper-preview-fit');
    this.element.append(this.page, this.count, this.zoom, fit);
  }
  update(state: ReaderState): void {
    this.element.hidden = !state.pages;
    if (document.activeElement !== this.page) this.page.value = String(state.page);
    this.page.max = String(state.pages); this.page.disabled = !state.pages;
    this.count.textContent = '/ ' + state.pages;
    if (document.activeElement !== this.zoom) this.zoom.value = Math.round(state.scale * 100) + '%';
    this.zoom.disabled = !state.pages;
  }
}

export function showReaderSearch(reader: PdfReader, host: HTMLElement, tr: Translator, signal: AbortSignal): void {
  const form = document.createElement('form'); const input = document.createElement('input');
  input.type = 'search'; input.maxLength = 256; input.setAttribute('aria-label', tr('preview.search'));
  input.placeholder = tr('preview.search');
  const submit = document.createElement('button'); submit.type = 'submit'; submit.textContent = tr('preview.search');
  form.append(input, submit); const results = document.createElement('div'); host.append(form, results); input.focus();
  let active: AbortController | null = null;
  form.addEventListener('submit', event => {
    event.preventDefault(); active?.abort();
    const request = new AbortController(); active = request;
    signal.addEventListener('abort', () => request.abort(), { once: true, signal: request.signal });
    results.textContent = tr('preview.searching');
    void reader.search(input.value, request.signal).then(found => {
      if (request.signal.aborted || active !== request) return;
      results.replaceChildren();
      for (const match of found.matches) results.append(readerButton(`${match.page}: ${match.excerpt}`,
        `${match.page} · ${match.excerpt}`, () => reader.goToPage(match.page), signal));
      if (!found.matches.length) results.textContent = tr('preview.noMatches');
      if (found.truncated) results.append(document.createTextNode(tr('preview.searchLimited')));
    }).catch(() => {
      if (!request.signal.aborted && active === request) results.textContent = tr('preview.searchFailed');
    }).finally(() => { request.abort(); if (active === request) active = null; });
  }, { signal });
}

export async function showReaderOutline(reader: PdfReader, host: HTMLElement, tr: Translator, signal: AbortSignal): Promise<void> {
  host.textContent = tr('common.loading');
  try {
    const items = await reader.outline();
    if (signal.aborted) return;
    host.replaceChildren();
    for (const item of items) {
      const row = readerButton(item.title, item.title, () => {
        void reader.goToDestination(item.destination).catch(() => {
          if (!signal.aborted) host.textContent = tr('preview.renderFailed');
        });
      }, signal);
      row.style.paddingInlineStart = (8 + item.depth * 12) + 'px'; host.append(row);
    }
    if (!items.length) host.textContent = tr('preview.noOutline');
  } catch { if (!signal.aborted) host.textContent = tr('preview.renderFailed'); }
}
