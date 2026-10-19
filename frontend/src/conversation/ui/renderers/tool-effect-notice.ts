/** DOM-local inspection of server-bound file publication evidence.
 * Entry: renderToolEffectNotice/disposeToolEffectNotice. Requests belong to the
 * mounted occurrence, abort on disposal/identity change, and never update Turns.
 */
import { createAlert, createButton } from '../../../ui';
import type { ToolExecutionEffect, ToolEffectInspectionReference,
  ConversationToolEffectInspection } from '../../../api/conversation-sync.generated';
import type { ToolEffectNotice } from '../../presentation/tool-effect-presentation';
import type { ClassicConversationRendererPorts } from './ports';
import { textFor } from './localized-copy';

interface NoticeState {
  key: string;
  kind: Exclude<ToolEffectNotice, null>;
  ports: ClassicConversationRendererPorts;
  reference?: ToolEffectInspectionReference;
  el?: HTMLElement;
  content?: string;
  button?: HTMLButtonElement;
  controller?: AbortController;
  observation?: ConversationToolEffectInspection;
  failed?: boolean;
}
const notices = new WeakMap<HTMLElement, NoticeState>();

export function disposeToolEffectNotice(node: HTMLElement): void {
  const state = notices.get(node);
  state?.controller?.abort();
  state?.el?.remove();
  notices.delete(node);
}

function copy(state: NoticeState): { title: string; body: string; variant: 'info' | 'warning' } {
  const text = (key: string, fallback: string) => textFor(state.ports, key, fallback);
  if (state.observation?.outcome === 'file_published') return {
    title: text('tool.effectPublished', 'File publication confirmed'),
    body: text('tool.effectPublishedBody', 'The journal proves this file content was published. Later edits or undo may have changed it. This does not verify the current file or accept the task.'), variant: 'info',
  };
  if (state.observation?.outcome === 'file_not_published') return {
    title: text('tool.effectUnpublished', 'File was not published when checked'),
    body: text('tool.effectUnpublishedBody', 'The original file and prepared evidence agree. Read the current file before planning another write, with normal approval.'), variant: 'info',
  };
  let body = state.kind === 'idempotent'
    ? text('tool.effectIdempotent', 'The call was interrupted. This tool declares that repeating the same operation is idempotent.')
    : text('tool.effectReconcile', 'The operation may have taken effect. Check the destination before deciding whether to retry.');
  if (state.failed) body = text('tool.effectInspectionFailed', 'Could not inspect the evidence. The outcome remains unknown. Retry this check, not the write.');
  else if (state.observation) {
    const reason = state.observation.reason;
    body = reason === 'writer_active'
      ? text('tool.effectWriterActive', 'A writer still holds the journal. The outcome remains unknown. Check again after it finishes.')
      : reason === 'evidence_conflict'
        ? text('tool.effectEvidenceConflict', 'The evidence conflicts with the recorded operation. The outcome remains unknown; review the file or request human guidance before another write.')
        : text('tool.effectEvidenceMissing', 'There is not enough trustworthy evidence to establish the outcome. Missing evidence does not mean the write failed.');
  }
  return { title: text('tool.effectUnknown', 'Operation outcome unknown'), body, variant: 'warning' };
}

function paint(node: HTMLElement, state: NoticeState): void {
  const value = copy(state);
  const content = JSON.stringify(value);
  if (!state.el || state.content !== content) {
    const focused = state.button === node.ownerDocument.activeElement;
    const alert = createAlert(value);
    alert.el.dataset.toolEffectNotice = state.kind;
    if (state.button) alert.el.appendChild(state.button);
    state.el?.remove();
    state.el = alert.el;
    state.content = content;
    node.appendChild(alert.el);
    if (focused) state.button?.focus();
  } else if (state.el.parentNode !== node) node.appendChild(state.el);
  state.el.setAttribute('aria-busy', String(Boolean(state.controller)));
  state.el.dataset.toolEffectOutcome = state.observation?.outcome ?? 'unknown';
  if (state.button) {
    // Keep keyboard focus while rejecting duplicate activation in the handler.
    state.button.setAttribute('aria-disabled', String(Boolean(state.controller)));
    state.button.textContent = state.controller
      ? textFor(state.ports, 'tool.effectChecking', 'Checking…')
      : state.observation || state.failed
        ? textFor(state.ports, 'tool.effectCheckAgain', 'Check again')
        : textFor(state.ports, 'tool.effectInspect', 'Check file publication');
  }
}

export function renderToolEffectNotice(node: HTMLElement, kind: ToolEffectNotice,
  ports: ClassicConversationRendererPorts, effect?: ToolExecutionEffect): void {
  if (!kind) { disposeToolEffectNotice(node); return; }
  const reference = effect?.scope === 'attempt' && effect.reconciliation?.kind === 'project_file_write'
    && effect.inspection?.operationId === effect.operationId ? effect.inspection : undefined;
  const key = JSON.stringify([kind, reference, effect?.operationId, effect?.reconciliation, Boolean(ports.inspectToolEffect)]);
  let state = notices.get(node);
  if (state && state.key !== key) { disposeToolEffectNotice(node); state = undefined; }
  if (!state) {
    state = { key, kind, ports, reference };
    notices.set(node, state);
  }
  state.ports = ports;
  if (reference && ports.inspectToolEffect && !state.button) {
    const current = state;
    current.button = createButton({ label: '', variant: 'secondary', size: 'sm', wrap: true,
      onClick: () => {
        if (current.controller || notices.get(node) !== current) return;
        const controller = new AbortController();
        current.controller = controller;
        current.failed = false;
        // Each explicit check replaces the old observation. No stale outcome
        // survives a later failed check or becomes a retry/acceptance grant.
        current.observation = undefined;
        paint(node, current);
        void (async () => {
          try {
            const result = await current.ports.inspectToolEffect!(reference, controller.signal);
            if (controller.signal.aborted || notices.get(node) !== current || !node.isConnected) return;
            if (result.conversationId !== reference.conversationId || result.turnId !== reference.turnId
                || result.operationId !== reference.operationId) throw new Error('Effect identity changed');
            current.observation = result;
          } catch {
            if (controller.signal.aborted || notices.get(node) !== current || !node.isConnected) return;
            current.failed = true;
          } finally {
            if (notices.get(node) === current && !controller.signal.aborted) {
              current.controller = undefined;
              if (node.isConnected) paint(node, current);
            }
          }
        })();
      },
    }, node.ownerDocument);
    current.button.dataset.toolEffectInspect = '';
    current.el?.appendChild(current.button);
  }
  paint(node, state);
}
