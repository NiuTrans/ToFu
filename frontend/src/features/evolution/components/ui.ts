/** Safe HTML presentation and stable keyed DOM for the Evolution page only. */
import { escapeHtml } from '../../../html-safety';
import { t, type I18nKey, type I18nParamsFor } from '../../../i18n';
export const esc = (value: unknown): string => escapeHtml(String(value ?? ''));
type UnparameterizedEvolutionKey = {
  [K in Extract<I18nKey, `evolution.${string}`>]: I18nParamsFor<K> extends never ? K : never
}[Extract<I18nKey, `evolution.${string}`>];
export const copy = (key: string): string => t(`evolution.page.${key}` as UnparameterizedEvolutionKey);
export const label = (status: string): string => {
  const known = ['success','failure','partial','aborted','running','proposed','confirmed','active','rejected','rolled_back','admitted'];
  return known.includes(status) ? t(`evolution.badge.${status}` as UnparameterizedEvolutionKey) : copy(status);
};
export const badge = (status: string): string => `<span class="evo-status" data-status="${esc(status)}">${esc(label(status))}</span>`;
export const button = (action: string, title: string, id = '', primary = false): string => `<button type="button" class="evo-button${primary ? ' evo-primary' : ''}" data-evo-action="${esc(action)}" data-id="${esc(id)}">${esc(title)}</button>`;
export const link = (path: string, title: string): string => `<a class="evo-link" href="#/evolution/${esc(path)}">${esc(title)}</a>`;
export const number = (value: number | undefined): string => value == null ? '—' : value.toLocaleString();
export const section = (title: string, body: string): string => `<section class="evo-section"><h2>${esc(title)}</h2>${body}</section>`;
export const empty = (title: string): string => `<p class="evo-empty">${esc(title)}</p>`;
export function patch(host: Element, html: string): void {
  const template = document.createElement('template'); template.innerHTML = html;
  reconcile(host, template.content);
}
function key(node: Node | undefined): string { return node?.nodeType === 1 ? (node as Element).getAttribute('data-key') || '' : ''; }
function reconcile(host: Node, source: Node): void {
  [...source.childNodes].forEach((fresh, index) => {
    let old = host.childNodes[index];
    const id = key(fresh);
    if (id && key(old) !== id) {
      const match = [...host.childNodes].find(n => key(n) === id);
      if (match) { host.insertBefore(match, old || null); old = match; }
    }
    if (!old) { host.appendChild(fresh.cloneNode(true)); return; }
    if (old.nodeName !== fresh.nodeName || key(old) !== id) { host.replaceChild(fresh.cloneNode(true), old); return; }
    if (old.nodeType !== 1 || fresh.nodeType !== 1) { if (old.textContent !== fresh.textContent) old.textContent = fresh.textContent; return; }
    const a = old as Element, b = fresh as Element;
    if (a.hasAttribute('data-evo-island')) return;
    const input = a.matches('input,textarea,select');
    for (const attr of [...a.attributes]) if (attr.name !== 'open' && !(input && attr.name === 'value') && !b.hasAttribute(attr.name)) a.removeAttribute(attr.name);
    for (const attr of [...b.attributes]) if (attr.name !== 'open' && !(input && attr.name === 'value') && a.getAttribute(attr.name) !== attr.value) a.setAttribute(attr.name, attr.value);
    if (!input) reconcile(a, b);
  });
  while (host.childNodes.length > source.childNodes.length) host.lastChild?.remove();
}
