/** One shell-owned review mount; existing conversation lifecycle retires it.
 * Dynamic loading is fenced by the active conversation and mount generation.
 * No polling, Turn state, review result cache or global feature registry.
 */
import type { TurnReviewCommand, TurnReviewView } from '../../api/artifact-review.generated';
import type { WorkingFactsView } from '../../api/working-facts.generated';
import type { TurnReviewPorts } from '../../rich-response/artifact-review';

export interface TurnReviewPresentationPorts {
  activeConversationId(): string | null;
  load(conversationId: string, turnId: string, signal: AbortSignal): Promise<TurnReviewView>;
  save(conversationId: string, turnId: string, command: TurnReviewCommand, signal: AbortSignal): Promise<TurnReviewView>;
  facts(conversationId: string, signal: AbortSignal): Promise<WorkingFactsView>;
  outputs(conversationId: string, signal: AbortSignal): Promise<Array<{ id: string; title: string }>>;
  openOutput(artifactId: string): void;
  text(key: string, fallback: string): string;
  reportError(message: string): void;
  loadDialog?(): Promise<{ openTurnReviewDialog(ports: TurnReviewPorts, onClosed: () => void): () => void }>;
}
export function createTurnReviewPresentation(ports: TurnReviewPresentationPorts) {
  let generation = 0;
  let target: { conversationId: string; turnId: string; execution?: string } | null = null;
  let closeDialog: (() => void) | null = null;
  let invalidate: (() => void) | null = null;
  let disposed = false;
  function close() { generation++; target = null; invalidate = null; const previous = closeDialog; closeDialog = null; previous?.(); }
  return {
    async open(conversationId: string, turnId: string) {
      if (disposed || ports.activeConversationId() !== conversationId) return;
      close(); target = { conversationId, turnId }; const current = generation;
      try {
        const feature = await (ports.loadDialog?.() ?? import('./dialog'));
        if (disposed || current !== generation || ports.activeConversationId() !== conversationId) return;
        closeDialog = feature.openTurnReviewDialog({
          load: signal => ports.load(conversationId, turnId, signal),
          save: (command, signal) => ports.save(conversationId, turnId, command, signal),
          loadFacts: signal => ports.facts(conversationId, signal),
          outputs: { load: signal => ports.outputs(conversationId, signal), open: ports.openOutput },
          subscribeInvalidation(listener) {
            if (disposed || current !== generation) return () => {};
            invalidate = listener;
            return () => { if (invalidate === listener) invalidate = null; };
          },
          text: ports.text,
        }, () => { if (current === generation) { generation++; target = null; closeDialog = null; invalidate = null; } });
      } catch (cause) {
        if (!disposed && current === generation) {
          close(); ports.reportError(ports.text('turnReview.openError', 'Task acceptance could not be opened') + ': ' + String(cause));
        }
      }
    },
    activate(conversationId: string | null) { if (target && target.conversationId !== conversationId) close(); },
    observe(conversationId: string, turns: Readonly<Record<string, unknown>>) {
      if (target?.conversationId !== conversationId) return;
      const turn = turns[target.turnId];
      if (!turn || typeof turn !== 'object') { close(); return; }
      const state = turn as Record<string, unknown>;
      const execution = JSON.stringify([state.currentAttemptId,
        state.modelContextRevision ?? state.projectionRevision, state.status]);
      if (execution !== target.execution) { target.execution = execution; invalidate?.(); }
    },
    disposeConversation(conversationId: string) { if (target?.conversationId === conversationId) close(); },
    destroy() { disposed = true; close(); },
  };
}
