/**
 * Application composition between the normalized TurnStore and UI surface.
 * Owns subscription/coalescing lifecycle only; state reduction stays domain-
 * owned and DOM reconciliation stays UI-owned.
 */
import type { TurnState, TurnStore, TurnStateUpdate } from '../domain/turn-store';
import {
  createConversationViewModelSelector,
  type ConversationViewModel,
  type ConversationViewModelDiagnostics,
} from '../presentation/conversation-view-model';
import type { ConversationSurface } from '../ui/conversation-surface';

export interface ConversationRenderScheduler {
  schedule(render: () => void): () => void;
}

export interface BindConversationSessionOptions {
  scheduler?: ConversationRenderScheduler;
  diagnostics?: ConversationViewModelDiagnostics;
  selectViewModel?(
    state: TurnState,
    diagnostics?: ConversationViewModelDiagnostics,
  ): ConversationViewModel;
}

export interface ConversationSessionBinding {
  renderNow(): void;
  dispose(): void;
}

const immediateScheduler: ConversationRenderScheduler = {
  schedule(render) {
    render();
    return () => {};
  },
};

/** Subscribe exactly once and coalesce store frames through an injected clock. */
export function bindConversationSession(
  store: TurnStore,
  surface: ConversationSurface,
  options: BindConversationSessionOptions = {},
): ConversationSessionBinding {
  const scheduler = options.scheduler ?? immediateScheduler;
  const selector = createConversationViewModelSelector();
  const select = options.selectViewModel ?? selector.select;
  let latestState = store.getState();
  let scheduled = false;
  let cancelScheduled: (() => void) | null = null;
  let disposed = false;

  const renderNow = (): void => {
    if (disposed) return;
    scheduled = false;
    cancelScheduled = null;
    surface.render(select(latestState, options.diagnostics));
  };
  const requestRender = (state: TurnState, update: TurnStateUpdate): void => {
    selector.observe(state, update);
    latestState = state;
    if (scheduled || disposed) return;
    scheduled = true;
    cancelScheduled = scheduler.schedule(renderNow);
  };
  const unsubscribe = store.subscribe(requestRender);
  renderNow();

  return {
    renderNow() {
      cancelScheduled?.();
      renderNow();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelScheduled?.();
      cancelScheduled = null;
      unsubscribe();
      selector.dispose();
      surface.dispose();
    },
  };
}
