import { presentTurnFinish, type TurnFinishPresentation } from './turn-presentation';
import type { ProjectionTurn } from './turn-projection';
import type { TurnState } from './turn-state';

export interface TurnRenderContext {
  commandPending: string | null;
  finish: TurnFinishPresentation | null;
}

export type TurnRenderer = (
  node: HTMLElement,
  turn: ProjectionTurn,
  context: TurnRenderContext,
) => void;

type ManagedTurnElement = HTMLElement & {
  _tofuTurnRenderKey?: string;
  _tofuTurnRenderer?: TurnRenderer | null;
};

/** Incrementally reconcile normalized turns without moving stable DOM nodes. */
export function renderTurnStateInto(
  container: Element,
  state: TurnState,
  renderTurn?: TurnRenderer,
): void {
  const wanted = Object.keys(state.laneOrder).sort().flatMap(
    (lane) => state.laneOrder[lane] ?? [],
  );
  const existing = new Map(
    Array.from(container.querySelectorAll<HTMLElement>('[data-turn-id]'))
      .map((node) => [node.dataset.turnId ?? '', node as ManagedTurnElement]),
  );
  const currentOrder = Array.from(container.children)
    .map((node) => (node as HTMLElement).dataset?.turnId)
    .filter((value): value is string => Boolean(value));
  const orderChanged = currentOrder.length !== wanted.length
    || wanted.some((turnId, index) => currentOrder[index] !== turnId);

  for (const turnId of wanted) {
    const turn = state.turnsById[turnId];
    if (!turn) continue;
    let node = existing.get(turnId);
    if (!node) {
      node = document.createElement('article') as ManagedTurnElement;
      node.dataset.turnId = turnId;
    }
    node.dataset.turnStatus = turn.status ?? '';
    const commandPending = state.commandPending[turnId] ?? null;
    const renderKey = [
      Number(turn.projectionRevision || 0),
      turn.status ?? '',
      turn.currentAttemptId ?? '',
      commandPending ?? '',
    ].join(':');
    const renderer = renderTurn ?? null;
    if (node._tofuTurnRenderKey !== renderKey
        || node._tofuTurnRenderer !== renderer) {
      if (renderTurn) {
        renderTurn(node, turn, {
          commandPending,
          finish: presentTurnFinish(turn),
        });
      } else {
        node.textContent = String(turn.projection?.content ?? '');
      }
      node._tofuTurnRenderKey = renderKey;
      node._tofuTurnRenderer = renderer;
    }
    if (orderChanged) container.appendChild(node);
    existing.delete(turnId);
  }
  for (const node of existing.values()) node.remove();
}
