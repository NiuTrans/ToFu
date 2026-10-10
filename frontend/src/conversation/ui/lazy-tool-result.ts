/**
 * DOM-local, abortable historical tool-output disclosure.
 * Entry points: bindLazyToolResult, loadedToolResult, disposeLazyToolResult.
 * Dependencies: injected generated detail transport/localization, UI factories.
 * Loaded detail lives only with its mounted block; it never patches TurnStore.
 */
import type { SnapshotToolResultReference, ConversationTurnToolResult } from '../../api/conversation-sync.generated';
import { createButton } from '../../ui/button';

export interface LazyToolResultLabels {
  loading(): string;
  retry(): string;
  failure(stale: boolean): string;
}

export type LazyToolResultLoader = (reference: SnapshotToolResultReference, signal: AbortSignal)
  => Promise<ConversationTurnToolResult>;

interface DetailState {
  key: string;
  reference: SnapshotToolResultReference;
  content?: string;
  controller?: AbortController;
  detach?: () => void;
  render: () => void;
}
const states = new WeakMap<HTMLElement, DetailState>();

function referenceFrom(value: unknown): SnapshotToolResultReference | null {
  if (value === undefined || value === null) return null;
  // The ordered coordinator already validates this declared generated field.
  // Do not import the browser transport just to inspect a presentation value.
  return value as SnapshotToolResultReference;
}
function referenceKey(reference: SnapshotToolResultReference): string {
  return JSON.stringify([reference.ownerScope, reference.conversationId, reference.turnId,
    reference.projectionRevision, reference.roundIndex, reference.toolCallId]);
}

export function disposeLazyToolResult(node: HTMLElement): void {
  const state = states.get(node);
  state?.controller?.abort();
  state?.detach?.();
  states.delete(node);
}

export function loadedToolResult(node: HTMLElement, value: unknown): string | undefined {
  const reference = referenceFrom(value);
  const state = states.get(node);
  if (!reference || !state || state.key !== referenceKey(reference)) {
    disposeLazyToolResult(node);
    return undefined;
  }
  return state.content;
}

export function bindLazyToolResult(
  node: HTMLElement, value: unknown, render: () => void, labels: LazyToolResultLabels, loadResult?: LazyToolResultLoader,
): void {
  const reference = referenceFrom(value);
  if (!reference) { disposeLazyToolResult(node); return; }
  const key = referenceKey(reference);
  let state = states.get(node);
  if (!state || state.key !== key) {
    disposeLazyToolResult(node);
    state = { key, reference, render };
    states.set(node, state);
  }
  state.render = render;
  state.detach?.();
  state.detach = undefined;
  const details = node.querySelector<HTMLDetailsElement>('details[data-lazy-tool-result]');
  if (!details || state.content !== undefined) return;
  const current = state;

  const load = async (): Promise<void> => {
    if (current.controller || current.content !== undefined || !details.open) return;
    const body = details.querySelector<HTMLElement>('.ptool-result-body');
    if (!body) return;
    const controller = new AbortController();
    current.controller = controller;
    body.setAttribute('aria-busy', 'true');
    const notice = node.ownerDocument.createElement('p');
    notice.setAttribute('role', 'status');
    notice.textContent = labels.loading();
    body.replaceChildren(notice);
    try {
      if (!loadResult) throw new Error('Tool result transport is unavailable');
      const result = await loadResult(reference, controller.signal);
      if (controller.signal.aborted || !node.isConnected || states.get(node) !== current) return;
      if (result.conversationId !== reference.conversationId || result.turnId !== reference.turnId
          || result.roundIndex !== reference.roundIndex
          || result.projectionRevision !== reference.projectionRevision
          || result.toolCallId !== reference.toolCallId) {
        throw new Error('Tool output identity changed');
      }
      current.content = result.toolContent;
      current.render();
    } catch (error) {
      if (controller.signal.aborted || !node.isConnected || states.get(node) !== current) return;
      const stale = (error as { status?: number })?.status === 409;
      notice.setAttribute('role', 'alert');
      notice.textContent = labels.failure(stale);
      body.replaceChildren(notice);
      if (!stale) body.appendChild(createButton({
        label: labels.retry(), size: 'sm', onClick: () => { void load(); },
      }, node.ownerDocument));
    } finally {
      if (current.controller === controller) {
        current.controller = undefined;
        body.removeAttribute('aria-busy');
      }
    }
  };
  const toggle = (): void => {
    if (details.open) { void load(); }
    else { current.controller?.abort(); current.controller = undefined; }
  };
  details.addEventListener('toggle', toggle);
  current.detach = () => details.removeEventListener('toggle', toggle);
  if (details.open) void load();
}
