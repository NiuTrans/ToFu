/** Source-linked citations over supplied read-only data, never remote preview fetching.
 * Entry: createCitationView. Dependencies: shared button kit, native dialog.
 * Source types derive from the authoritative rich-response schema.
 */
import { createButton, createDialog } from '../ui';

import type { citationSource, citations } from './contract.generated';
export type CitationSource = citationSource;
export type CitationBlock = citations;
export interface CitationPorts { text?(key: string, fallback: string): string }
export interface CitationMount { dispose(): void }
export interface CitationView {
  update(blocks: readonly CitationBlock[]): void;
  renderBlock(host: HTMLElement, blockId: string): CitationMount;
  /** Plain authored prose only; callers must not pass code/HTML/link text. */
  renderInline(host: HTMLElement, text: string): CitationMount;
  dispose(): void;
}
const ID = /^[A-Za-z0-9_-]{1,64}$/;
const TOKEN = /\[\[cite:([A-Za-z0-9_-]{1,64})\/([A-Za-z0-9_-]{1,64})\]\]/g;

/** No relative, credentialed, executable or browser-normalized ambiguous URLs. */
export function safeCitationUrl(value: string): string | null {
  if (typeof value !== 'string' || value.length > 2048 || !/^https?:\/\//i.test(value)
    || /[\s\u0000-\u001f\u007f\\]/u.test(value)) return null;
  try {
    const url = new URL(value);
    return (url.protocol === 'https:' || url.protocol === 'http:') && url.hostname
      && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

export function createCitationView(owner: HTMLElement, ports: CitationPorts = {}): CitationView {
  const doc = owner.ownerDocument;
  const text = (key: string, fallback: string) => ports.text?.(key, fallback) ?? fallback;
  const element = (tag: string, value = '', className = '') => {
    const node = doc.createElement(tag);
    node.textContent = value;
    node.className = className;
    return node;
  };
  let blocks = new Map<string, CitationBlock>();
  let disposed = false;
  const mounts = new Set<{ refresh(): void; dispose(): void }>();
  let preview: { el: HTMLDialogElement; trigger: HTMLButtonElement; close(): void; key: string; identity: string } | null = null;
  const sourceAt = (blockId: string, sourceId: string) => blocks.get(blockId)?.sources.find(source => source.id === sourceId);
  const show = (blockId: string, sourceId: string, trigger: HTMLButtonElement) => {
    if (disposed) return;
    preview?.close();
    const source = sourceAt(blockId, sourceId);
    const url = source && safeCitationUrl(source.url);
    if (!source || !url) return;
    const body = element('div', '', 'rich-citation-preview');
    if (source.publisher) body.append(element('p', source.publisher));
    if (source.excerpt) body.append(element('blockquote', source.excerpt));
    else body.append(element('p', text('rich.citationNoExcerpt', 'No excerpt supplied.')));
    // The kit currently creates in the global realm; adopt its complete subtree
    // into the mounting document before opening, preserving native semantics.
    const handle = createDialog({ title: source.title, body });
    const dialog = doc.adoptNode(handle.el);
    dialog.setAttribute('aria-label', source.title);
    const link = doc.createElement('a');
    link.textContent = text('rich.citationOpen', 'Open source') + ' · ' + url;
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    body.append(link);
    const closeButton = createButton({ label: text('rich.citationClose', 'Close'), size: 'sm' }, doc);
    body.append(closeButton);
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      closeButton.removeEventListener('click', close);
      dialog.removeEventListener('cancel', cancel);
      dialog.removeEventListener('close', close);
      handle.close();
      dialog.remove();
      preview = null;
      trigger.setAttribute('aria-expanded', 'false');
      if (trigger.isConnected) trigger.focus();
    };
    const cancel = (event: Event) => { event.preventDefault(); close(); };
    closeButton.addEventListener('click', close);
    dialog.addEventListener('cancel', cancel);
    dialog.addEventListener('close', close);
    doc.body.append(dialog);
    preview = { el: dialog, trigger, close, key: `${blockId}/${sourceId}`, identity: JSON.stringify(source) };
    trigger.setAttribute('aria-expanded', 'true');
    try { void handle.open(); closeButton.focus(); } catch { close(); }
  };
  const chip = (blockId: string, sourceId: string) => {
    const wrap = element('span', '', 'rich-citation-chip');
    const button = createButton({ label: '', variant: 'ghost', size: 'sm' }, doc);
    button.dataset.citationRef = `${blockId}/${sourceId}`;
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    const activate = () => show(blockId, sourceId, button);
    button.addEventListener('click', activate);
    wrap.append(button);
    return { node: wrap, refresh() {
      const source = sourceAt(blockId, sourceId);
      const available = !!source && !!safeCitationUrl(source.url);
      const label = available ? source.title : text('rich.citationUnavailable', 'Source unavailable');
      // The shared kit intentionally keeps buttons single-line. Short chip copy
      // fits narrow panes; the full supplied title remains in aria-label/preview.
      const characters = Array.from(label);
      const visible = available && characters.length > 16 ? characters.slice(0, 16).join('') + '…' : label;
      if (button.textContent !== visible) button.textContent = visible;
      button.disabled = !available;
      button.setAttribute('aria-label', text('rich.citationPreview', 'Source preview') + ': ' + label + ` (${sourceId})`);
    }, dispose() { button.removeEventListener('click', activate); button.disabled = true; } };
  };
  const register = (host: HTMLElement, refresh: () => void, cleanup: () => void): CitationMount => {
    let released = false;
    const mount = { refresh, dispose() {
      if (released) return;
      released = true;
      // A removed rendering cannot retain an active preview or its trigger.
      if (preview && host.contains(preview.trigger)) preview.close();
      cleanup(); mounts.delete(mount);
    } };
    mounts.add(mount); refresh(); return mount;
  };
  return {
    update(values) {
      if (disposed) return;
      const next = new Map<string, CitationBlock>();
      // Fail closed on ambiguous identities; never guess among duplicate sources.
      const counts = new Map<string, number>();
      values.forEach(block => counts.set(block.id, (counts.get(block.id) ?? 0) + 1));
      for (const block of values) {
        if (!ID.test(block.id) || counts.get(block.id) !== 1) continue;
        const ids = new Set<string>();
        if (block.sources.some(source => {
          const invalid = !ID.test(source.id) || ids.has(source.id)
            || typeof source.title !== 'string' || !source.title.length || source.title.length > 1000
            || (source.publisher !== undefined && typeof source.publisher !== 'string')
            || (source.excerpt !== undefined && typeof source.excerpt !== 'string')
            || (source.publisher?.length ?? 0) > 1000 || (source.excerpt?.length ?? 0) > 4000;
          ids.add(source.id);
          return invalid;
        })) continue;
        next.set(block.id, { ...block, sources: block.sources.map(source => ({ ...source })) });
      }
      blocks = next;
      if (preview) {
        const [blockId, sourceId] = preview.key.split('/');
        if (JSON.stringify(sourceAt(blockId, sourceId)) !== preview.identity) preview.close();
      }
      mounts.forEach(mount => mount.refresh());
    },
    renderInline(host, value) {
      if (disposed) return { dispose() {} };
      const chips: ReturnType<typeof chip>[] = [];
      const fragment = doc.createDocumentFragment();
      let end = 0;
      for (const match of value.matchAll(TOKEN)) {
        fragment.append(doc.createTextNode(value.slice(end, match.index)));
        const control = chip(match[1], match[2]); chips.push(control); fragment.append(control.node);
        end = match.index + match[0].length;
      }
      fragment.append(doc.createTextNode(value.slice(end)));
      host.replaceChildren(fragment);
      return register(host, () => chips.forEach(control => control.refresh()), () => chips.forEach(control => control.dispose()));
    },
    renderBlock(host, blockId) {
      if (disposed) return { dispose() {} };
      const heading = element('h3');
      const list = element('div', '', 'rich-citation-list');
      host.replaceChildren(heading, list);
      const chips = new Map<string, ReturnType<typeof chip>>();
      return register(host, () => {
        const block = blocks.get(blockId);
        heading.textContent = block?.title ?? text('rich.citationUnavailable', 'Source unavailable');
        const active = new Set(block?.sources.map(source => source.id) ?? []);
        chips.forEach((control, id) => { if (!active.has(id)) { control.dispose(); control.node.remove(); chips.delete(id); } });
        for (const source of block?.sources ?? []) {
          let control = chips.get(source.id);
          if (!control) { control = chip(blockId, source.id); chips.set(source.id, control); }
          control.refresh();
          // Avoid moving unchanged focused nodes during snapshot replay.
          const index = block!.sources.indexOf(source);
          if (list.children[index] !== control.node) list.insertBefore(control.node, list.children[index] ?? null);
        }
      }, () => chips.forEach(control => control.dispose()));
    },
    dispose() {
      if (disposed) return;
      disposed = true; preview?.close(); mounts.forEach(mount => mount.dispose()); blocks.clear();
    },
  };
}
