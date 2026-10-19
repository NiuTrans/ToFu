/** Keyed rich-fence and supplied-source prose mounting; original data owns evidence. */
import { splitRichMarkdown, parseRichResponse } from './validation';
import { renderRichResponse, disposeRichResponse } from './renderer';
import { createCitationView, type CitationView, type CitationMount } from './citations';
import type { ClassicConversationRendererPorts } from '../conversation/ui/renderers/ports';

import type { ChoicePorts } from './choices';
import { setCompletedMarkdownHtml, setStreamingMarkdownHtml, resetManagedHtml } from '../conversation/ui/renderers/stable-dom';
interface ProseMount { identity: string; mounts: CitationMount[] }
interface State { nodes: Map<string, HTMLElement>; citations: CitationView; prose: Map<string, ProseMount>; choices?: ChoicePorts; scope: string }
const states = new WeakMap<HTMLElement, State>();
export function disposeRichMarkdown(host: HTMLElement): void {
  const state = states.get(host);
  if (state) { state.nodes.forEach(disposeRichResponse); state.citations.dispose(); }
  states.delete(host);
}
function mountProseReferences(host: HTMLElement, view: CitationView): CitationMount[] {
  const doc = host.ownerDocument, mounts: CitationMount[] = [];
  const walker = doc.createTreeWalker(host, 4);
  const nodes: Text[] = [];
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    if (node.textContent?.includes('[[cite:') && !node.parentElement?.closest('pre,code,a,button,script,style,textarea,svg,math')) nodes.push(node);
  }
  for (const node of nodes) {
    const span = doc.createElement('span'); span.className = 'rich-citation-inline';
    node.replaceWith(span); mounts.push(view.renderInline(span, node.textContent ?? ''));
  }
  return mounts;
}
export function renderRichMarkdown(host: HTMLElement, original: string, terminal: boolean, ports: ClassicConversationRendererPorts, conversationId?: string, turnId?: string): boolean {
  const parts = splitRichMarkdown(original);
  if (!parts.some(p => p.kind !== 'markdown')) { if (states.has(host)) { disposeRichMarkdown(host); host.replaceChildren(); resetManagedHtml(host); } return false; }
  let state = states.get(host);
  const scope = JSON.stringify([conversationId, turnId]);
  if (state && state.scope !== scope) { disposeRichMarkdown(host); state = undefined; }
  if (!state) {
    host.replaceChildren(); resetManagedHtml(host);
    state = { nodes: new Map(), citations: createCitationView(host, { text: (key, fallback) => ports.localizedText?.(key, fallback) ?? fallback }), prose: new Map(), scope,
      choices: conversationId && turnId ? ports.choiceInteractions?.(conversationId, turnId) : undefined };
    states.set(host, state);
  }
  state.citations.update(parts.flatMap(part => part.kind === 'rich'
    ? (parseRichResponse(part.source)?.blocks.filter(block => block.type === 'citations') ?? []) : []));
  const live = new Set<string>();
  let cursor: ChildNode | null = null;
  parts.forEach((part,index) => {
    const key = `${index}:${part.kind === 'markdown' ? 'markdown' : 'fence'}`; live.add(key);
    let el = state!.nodes.get(key);
    if (!el) { el = host.ownerDocument.createElement('div'); state!.nodes.set(key,el); }
    if (part.kind === 'markdown') {
      const html = ports.renderSafeMarkdownHtml(part.source, terminal ? undefined : {streaming:true});
      const identity = JSON.stringify([html, terminal]);
      const previous = state!.prose.get(key);
      if (previous?.identity !== identity) {
        previous?.mounts.forEach(mount => mount.dispose());
        // Managed-html identity must not reuse DOM mutated by inline citation mounts.
        resetManagedHtml(el);
        (terminal ? setCompletedMarkdownHtml : setStreamingMarkdownHtml)(el, html);
        state!.prose.set(key, { identity, mounts: mountProseReferences(el, state!.citations) });
      }
    }
    else if (part.kind === 'partial') { disposeRichResponse(el); el.className = 'rich-pending'; el.textContent = `${ports.localizedText?.('rich.pending','Rich response is still arriving…') ?? 'Rich response is still arriving…'}\n${part.source.slice(0,8000)}`; }
    else {
      el.className = 'rich-response';
      if (!renderRichResponse(el, part.source, { conversationId, surfaceContext: conversationId && turnId ? {conversationId,turnId} : undefined, choices: state!.choices, text: (key, fallback) => ports.localizedText?.(key, fallback) ?? fallback })) {
        const fallback = host.ownerDocument.createElement('pre');
        fallback.className = 'rich-pending';
        fallback.textContent = part.source.slice(0, 8000) + (part.source.length > 8000 ? '\n[Source preview omitted after 8,000 characters]' : '');
        el.append(fallback);
      }
    }
    if (cursor ? cursor.nextSibling !== el : host.firstChild !== el) { if (cursor) cursor.after(el); else host.prepend(el); } cursor = el;
  });
  for (const [key,el] of state.nodes) if (!live.has(key)) {
    state.prose.get(key)?.mounts.forEach(mount => mount.dispose()); state.prose.delete(key);
    disposeRichResponse(el); el.remove(); state.nodes.delete(key);
  }
  return true;
}
