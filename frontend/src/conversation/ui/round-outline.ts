/**
 * Turn-local action evidence states. Each keyed tool/program owns its summary;
 * this controller never creates controls or hides authored content.
 * Entry points: update, patchBlock, completeBlock. No backend or transcript state.
 */
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../presentation/conversation-view-model';
import { toolBlockOwnsSummary, toolBlockActivityState, type ActionActivityState } from '../presentation/round-outline';
import { hasAcceptedWorkUnit } from '../presentation/tool-execution-groups';

export type RoundOutlineLabel = `roundOutline.${'active' | 'background' | 'attention' | 'returned'}`;
export type RoundOutlineTranslator = (key: RoundOutlineLabel, count?: number) => string;
const english: RoundOutlineTranslator = (key, n) => ({
  'roundOutline.active': 'In progress', 'roundOutline.background': `${n} in background`,
  'roundOutline.attention': 'Review details', 'roundOutline.returned': 'Response returned',
})[key];

type MountedAction = { state: ActionActivityState; initialized?: boolean };

/** Adjacent operational leaves share a visual surface. This is decoration of
 * the mounted sequence, never grouping, reparenting or disclosure ownership.
 * Durable deliverables and authored narration always interrupt the surface. */
function isProcessLeaf(block: ConversationBlockViewModel): boolean {
  if (block.kind === 'tool' || block.kind === 'program') return !hasAcceptedWorkUnit(block.round);
  return ['thinking', 'injections', 'system-note', 'system-note-group',
    'compaction', 'activity-event', 'activity-event-group', 'file-changes'].includes(block.kind);
}

type ProcessEdge = 'single' | 'start' | 'middle' | 'end';

export class RoundOutline {
  private readonly actions = new Map<string, MountedAction>();
  private readonly processEdges = new Map<string, ProcessEdge>();
  private readonly translate: RoundOutlineTranslator;

  constructor(_container: HTMLElement, translate?: RoundOutlineTranslator) {
    this.translate = translate ?? english;
  }

  private evidenceDisclosure(node: HTMLElement): HTMLDetailsElement | null {
    const details = node.querySelector<HTMLDetailsElement>('details');
    // A swarm panel is already the outer evidence card. Its first disclosure
    // contains task instructions, not this action's result; only its own
    // keyed controls may change internal task/history/error disclosure state.
    return details && !details.closest('.sw-panel') ? details : null;
  }

  update(turn: ConversationTurnViewModel, blocks: ReadonlyArray<ConversationBlockViewModel>): void {
    const next = new Map<string, MountedAction>();
    this.processEdges.clear();
    if (turn.role === 'assistant') {
      const process = blocks.map(isProcessLeaf);
      blocks.forEach((block, index) => {
        if (process[index]) this.processEdges.set(block.blockId,
          process[index - 1] ? (process[index + 1] ? 'middle' : 'end')
            : (process[index + 1] ? 'start' : 'single'));
      });
      for (const block of blocks) {
        if (!toolBlockOwnsSummary(block)) continue;
        const item = this.actions.get(block.blockId) ?? { state: 'returned' as const };
        item.state = toolBlockActivityState(block);
        next.set(block.blockId, item);
      }
    }
    this.actions.clear();
    for (const [id, item] of next) this.actions.set(id, item);
  }

  patchBlock(node: HTMLElement, block: ConversationBlockViewModel): void {
    node.dataset.deliverable = String(block.kind === 'text' && block.deliverable);
    const edge = this.processEdges.get(block.blockId);
    if (edge) node.dataset.processEdge = edge;
    else delete node.dataset.processEdge;
    const item = this.actions.get(block.blockId);
    if (item) {
      node.dataset.toolSummary = 'self-contained';
      node.dataset.actionState = item.state;
    } else {
      delete node.dataset.toolSummary;
      delete node.dataset.actionState;
    }
    // Visibility is owned by the content window, never internal round identity.
    node.hidden = false;
  }

  completeBlock(node: HTMLElement, block: ConversationBlockViewModel): void {
    const item = this.actions.get(block.blockId);
    if (!item) return;
    if (!item.initialized) {
      const details = this.evidenceDisclosure(node);
      // Actionable evidence opens on first paint, except generic raw failures:
      // their concise diagnostic is already visible in the sole summary. Keep
      // raw envelopes folded; replays never override the reader's choice.
      if (details && item.state !== 'returned'
          && details.dataset.toolFailureEvidence !== 'collapsed') details.open = true;
      item.initialized = true;
    }
    // Generic results otherwise lack a verdict. Merge it into their sole row;
    // commands, approvals and rich warning cards already own explicit states.
    const summary = node.querySelector<HTMLElement>('.ptool-result-header');
    const prior = summary?.querySelector<HTMLElement>('.conversation-action-state');
    if (!summary || item.state === 'returned') { prior?.remove(); return; }
    const state = prior ?? node.ownerDocument.createElement('span');
    state.className = 'conversation-action-state';
    state.dataset.state = item.state;
    state.textContent = this.translate(`roundOutline.${item.state}`, 1);
    if (item.state === 'active') state.setAttribute('role', 'status');
    else state.removeAttribute('role');
    if (!prior) summary.append(state);
  }
}
