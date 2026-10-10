/**
 * Turn-local action evidence navigation. Each keyed tool/program owns its summary;
 * this controller never creates a model-round heading or hides authored content.
 * Entry points: update, patchBlock, completeBlock. No backend or transcript state.
 */
import { createButton } from '../../ui';
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../presentation/conversation-view-model';
import { toolBlockOwnsSummary, toolBlockActivityState, type ActionActivityState } from '../presentation/round-outline';

export type RoundOutlineLabel = `roundOutline.${'activity' | 'round' | 'attempt' | 'tools'
  | 'active' | 'background' | 'attention' | 'returned'
  | 'expandAll' | 'collapseCompleted' | 'latest' | 'details'}`;
export type RoundOutlineTranslator = (key: RoundOutlineLabel, count?: number) => string;
const english: RoundOutlineTranslator = (key, n) => ({
  'roundOutline.activity': 'Activity', 'roundOutline.round': `Round ${n}`,
  'roundOutline.attempt': `Execution ${n}`, 'roundOutline.tools': `${n} tool${n === 1 ? '' : 's'}`,
  'roundOutline.active': 'In progress', 'roundOutline.background': `${n} in background`,
  'roundOutline.attention': 'Review details', 'roundOutline.returned': 'Response returned',
  'roundOutline.expandAll': 'Expand all', 'roundOutline.collapseCompleted': 'Collapse completed',
  'roundOutline.latest': 'Latest activity', 'roundOutline.details': 'View tool details',
})[key];

type MountedAction = { state: ActionActivityState; node?: HTMLElement; initialized?: boolean };

export class RoundOutline {
  readonly toolbar: HTMLElement;
  private readonly actions = new Map<string, MountedAction>();
  private readonly translate: RoundOutlineTranslator;

  constructor(private readonly container: HTMLElement, translate?: RoundOutlineTranslator) {
    this.translate = translate ?? english;
    this.toolbar = container.ownerDocument.createElement('nav');
    this.toolbar.className = 'conversation-round-toolbar';
    this.toolbar.setAttribute('aria-label', this.translate('roundOutline.activity'));
    // Navigation is context, not a second title above the turn's content.
    for (const action of ['expandAll', 'collapseCompleted', 'latest'] as const) {
      const button = createButton({
        label: this.translate(`roundOutline.${action}`), variant: 'ghost', size: 'sm',
        onClick: () => this.act(action),
      }, container.ownerDocument);
      button.dataset.roundAction = action;
      this.toolbar.append(button);
    }
  }

  private evidenceDisclosure(node: HTMLElement): HTMLDetailsElement | null {
    const details = node.querySelector<HTMLDetailsElement>('details');
    // A swarm panel is already the outer evidence card. Its first disclosure
    // contains task instructions, not this action's result; only its own
    // keyed controls may change internal task/history/error disclosure state.
    return details && !details.closest('.sw-panel') ? details : null;
  }

  private setEvidence(node: HTMLElement, open: boolean): void {
    // Native cards (generic, write, checklist, fallback, program) own disclosure.
    // Bulk controls operate their outer evidence fold, never the block shell.
    const details = this.evidenceDisclosure(node);
    if (details) details.open = open;
    // Terminal/browser cards deliberately keep code and output independent.
    // Use the existing delegated controls so their interaction-key owner stays
    // authoritative across settlement/replay instead of inventing another set.
    for (const button of node.querySelectorAll<HTMLButtonElement>('.ptool-cmd-fold[aria-expanded]')) {
      if ((button.getAttribute('aria-expanded') === 'true') !== open) button.click();
    }
  }

  private act(action: 'expandAll' | 'collapseCompleted' | 'latest'): void {
    if (action === 'latest') {
      const items = [...this.actions.values()].filter(item => item.node?.isConnected);
      const target = items.filter(item => item.state !== 'returned').at(-1) ?? items.at(-1);
      const node = target?.node;
      if (!node) return;
      this.setEvidence(node, true);
      const control = node.querySelector<HTMLElement>('summary, .ptool-cmd-fold, button, a[href]');
      if (control) control.focus({ preventScroll: true });
      else {
        node.tabIndex = -1;
        node.focus({ preventScroll: true });
      }
      node.scrollIntoView?.({ block: 'nearest', behavior: 'auto' });
      return;
    }
    for (const item of this.actions.values()) {
      if (item.node && (action === 'expandAll' || item.state === 'returned')) {
        this.setEvidence(item.node, action === 'expandAll');
      }
    }
  }

  update(turn: ConversationTurnViewModel, blocks: ReadonlyArray<ConversationBlockViewModel>): void {
    const next = new Map<string, MountedAction>();
    if (turn.role === 'assistant') {
      for (const block of blocks) {
        if (!toolBlockOwnsSummary(block)) continue;
        const item = this.actions.get(block.blockId) ?? { state: 'returned' as const };
        item.state = toolBlockActivityState(block);
        next.set(block.blockId, item);
      }
    }
    this.actions.clear();
    for (const [id, item] of next) this.actions.set(id, item);
    this.toolbar.hidden = this.actions.size < 4;
  }

  patchBlock(node: HTMLElement, block: ConversationBlockViewModel): void {
    node.dataset.deliverable = String(block.kind === 'text' && block.deliverable);
    const item = this.actions.get(block.blockId);
    if (item) {
      item.node = node;
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
      // Attention evidence is visible on first paint. Replays never undo a
      // reader's open choice, including after active -> returned transitions.
      if (details && item.state !== 'returned') details.open = true;
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
