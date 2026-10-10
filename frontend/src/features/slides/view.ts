/** Editor shell and thumbnail projection. Controls come from the shared UI factories. */
import { createButton } from '../../ui';
import type { SlidesApi, Translate } from './contracts';

export function createEditorView(doc: Document, t: Translate) {
  const node = (tag: string, className: string, text = '') => {
    const result = doc.createElement(tag); result.className = className; result.textContent = text; return result;
  };
  const overlay = node('div', 'sled-overlay');
  const shell = node('div', 'sled-shell'); shell.setAttribute('role', 'dialog'); shell.setAttribute('aria-modal', 'true');
  shell.setAttribute('aria-label', t('sled.title'));
  const head = node('div', 'sled-head');
  const title = node('span', 'sled-title', t('sled.title'));
  const indicator = node('span', 'sled-page-indicator');
  const raw = doc.createElement('a'); raw.className = 'sled-open-raw'; raw.target = '_blank'; raw.rel = 'noopener';
  raw.textContent = t('sled.openRaw');
  const close = createButton({ label: '×', variant: 'ghost', size: 'sm' }, doc); close.classList.add('sled-close');
  close.setAttribute('aria-label', t('sled.close'));
  head.append(title, indicator, raw, close);
  const body = node('div', 'sled-body'), rail = node('div', 'sled-rail'), canvas = node('div', 'sled-canvas');
  const frame = doc.createElement('iframe'); frame.className = 'sled-canvas-frame'; frame.title = t('sled.title');
  const side = node('div', 'sled-side'); canvas.append(frame); body.append(rail, canvas, side);
  shell.append(head, body); overlay.append(shell);
  return { overlay, shell, rail, frame, side, indicator, raw, close };
}
export type EditorView = ReturnType<typeof createEditorView>;

export function renderPageChrome(view: EditorView, api: SlidesApi, taskId: string, page: number, total: number, t: Translate): void {
  const doc = view.overlay.ownerDocument;
  view.rail.replaceChildren();
  for (let n = 1; n <= total; n++) {
    const thumb = createButton({ label: '', variant: 'ghost', size: 'sm' }, doc);
    thumb.classList.add('sled-rail-thumb'); thumb.classList.toggle('sled-rail-thumb--active', n === page);
    thumb.dataset.sledPage = String(n); thumb.setAttribute('aria-label', String(n)); thumb.setAttribute('aria-current', n === page ? 'page' : 'false');
    const image = doc.createElement('img'); image.loading = 'lazy'; image.alt = String(n);
    image.src = api.pageFileUrl(taskId, n, Date.now() % 100000);
    const label = doc.createElement('span'); label.textContent = String(n); thumb.append(image, label); view.rail.append(thumb);
  }
  view.indicator.textContent = t('sled.pageOf', { n: page, total });
  view.raw.href = api.pageFileUrl(taskId, page, 1);
}
