/** Retire Surface-owned presentation references on detached DOM. Renderer
 * request/listener teardown runs first at the Surface's disposal port. No
 * transport, history policy or renderer-private properties are owned here. */
import type {
  ManagedBlockElement, ManagedBlocksContainerElement, ManagedTurnAvatarElement,
  ManagedTurnHeaderElement, ManagedTurnActionsElement, ManagedTurnFooterElement,
  ManagedTurnContextRailElement, ManagedPlanDecisionElement, ManagedTurnElement,
  ManagedLaneHeaderElement, ManagedQueueElement,
} from './surface-dom';

type ManagedNode = ManagedBlockElement & ManagedBlocksContainerElement
  & ManagedTurnAvatarElement & ManagedTurnHeaderElement & ManagedTurnActionsElement
  & ManagedTurnFooterElement & ManagedTurnContextRailElement & ManagedPlanDecisionElement
  & ManagedTurnElement & ManagedLaneHeaderElement & ManagedQueueElement;

const PRESENTATION_REFERENCES = [
  '_tofuBlock', '_tofuBlockRenderer', '_tofuRoundOutline', '_tofuBlocksSource',
  '_tofuBlocksRenderer', '_tofuTurn', '_tofuTurnAvatarRenderer',
  '_tofuTurnHeaderRenderer', '_tofuTurnActionsRenderer', '_tofuTurnFooterRenderer',
  '_tofuContextBlock', '_tofuTurnContextRailRenderer', '_tofuPlanDecision',
  '_tofuPlanDecisionRenderer', '_tofuTurnVm', '_tofuTurnEntries', '_tofuTurnPlanDecision',
  '_tofuTurnSurfaceOptions', '_tofuLane', '_tofuLaneHeaderRenderer',
  '_tofuQueueItem', '_tofuQueueRenderer', '_tofuQueueContext',
] as const satisfies ReadonlyArray<keyof ManagedNode>;

/** Required cleanup errors still reach the caller, after every sibling has
 * retired. A boolean also preserves a deliberately thrown undefined/null. */
export function finishSurfaceCleanup(steps: Iterable<() => void>): void {
  let failed = false;
  let firstError: unknown;
  for (const step of steps) {
    try { step(); }
    catch (error) { if (!failed) { failed = true; firstError = error; } }
  }
  if (failed) throw firstError;
}

function release(node: ManagedNode): void {
  node._tofuRoundOutline?.dispose();
  for (const key of PRESENTATION_REFERENCES) delete node[key];
}

export function releaseSurfacePresentation(node: HTMLElement): void {
  release(node);
  for (const child of node.querySelectorAll<HTMLElement>(
    '[data-conversation-part], [data-presentation-id], [data-block-id], [data-lane-id], [data-queue-id]',
  )) release(child);
}
