/**
 * Turn-local round overview and reader choices. Existing keyed tool nodes keep
 * their evidence owners; the overview never hides a mounted call.
 * No backend, transport or transcript state. Content mounting stays in Surface.
 */
import type { ConversationBlockViewModel, ConversationTurnViewModel } from '../presentation/conversation-view-model';
import type { GroupedTurnEntry } from '../presentation/grouped-turn-layout';
import { toolBlockOwnsSummary, toolBlockActivityState, selectProgramBatches, selectRoundOutline,
  type ActionActivityState, type ProgramBatchSection, type RoundOutlineSection } from '../presentation/round-outline';
import { programBatchLabelParts, type ProgramBatchLabel } from '../presentation/program-batch-presentation';
import { hasAcceptedWorkUnit } from '../presentation/tool-execution-groups';
import { toolSummaryIdentity } from '../presentation/tool-summary-identity';
import { decorateToolSummary } from './tool-summary';
import { disclosureChevron } from './renderers/icons';
import { setStableData as setData } from './renderers/stable-dom';

export type RoundOutlineLabel = `roundOutline.${'active' | 'background' | 'attention' | 'returned'
  | 'activity' | 'round' | 'tools' | 'search' | 'matches' | 'moreMatches' | 'previousMatches'}`
  | ProgramBatchLabel;
export type RoundOutlineTranslator = (key: RoundOutlineLabel, count?: number) => string;
const english: RoundOutlineTranslator = (key, n) => ({
  'roundOutline.active': 'In progress', 'roundOutline.background': `${n} in background`,
  'roundOutline.attention': 'Review details', 'roundOutline.returned': 'Response returned',
  'roundOutline.activity': 'Execution', 'roundOutline.round': `Round ${n}`,
  'roundOutline.tools': `${n} tool calls`, 'roundOutline.search': 'Find a call',
  'roundOutline.matches': `${n} matching calls`, 'roundOutline.moreMatches': 'More matches',
  'roundOutline.previousMatches': 'Previous matches',
  'roundOutline.programs': `Programs: ${n}`, 'roundOutline.programsRunning': `${n} running`,
  'roundOutline.programsWaiting': `${n} waiting`, 'roundOutline.programsCompleted': `${n} completed`,
  'roundOutline.programsReturned': `${n} returned`,
  'roundOutline.programsHandoff': `${n} returned for model judgment`,
  'roundOutline.programsFailed': `${n} failed`, 'roundOutline.programsIncomplete': `${n} incomplete`,
})[key];

/* `revealed` is the disclosure first paint opened; a program batch that later
 * claims the block withdraws that reveal from its card. */
type MountedAction = { state: ActionActivityState; initialized?: boolean; revealed?: HTMLDetailsElement };

/** Adjacent operational leaves share a visual surface. This is decoration of
 * the mounted sequence, never grouping, reparenting or disclosure ownership.
 * Durable deliverables, authored narration and the tool panel header always
 * interrupt the surface. */
function isProcessLeaf(block: GroupedTurnEntry): boolean {
  if (block.kind === 'tool' || block.kind === 'program') return !hasAcceptedWorkUnit(block.round);
  return ['thinking', 'injections', 'system-note', 'system-note-group',
    'compaction', 'activity-event', 'activity-event-group', 'file-changes'].includes(block.kind);
}

type ProcessEdge = 'single' | 'start' | 'middle' | 'end';
type RoundControls = {
  section: RoundOutlineSection; header: HTMLElement; label: HTMLElement;
  search: HTMLInputElement; results: HTMLElement;
  blocks: ConversationBlockViewModel[]; limit: number;
};
/* One head per multi-program batch. `choice` is the reader's explicit fold;
 * without one the batch follows its presentation default as outcomes arrive. */
type ProgramBatchControls = {
  section: ProgramBatchSection; head: HTMLButtonElement; label: HTMLElement;
  choice?: boolean; expanded: boolean; labelKey: string;
};

export class RoundOutline {
  private readonly actions = new Map<string, MountedAction>();
  private readonly processEdges = new Map<string, ProcessEdge>();
  private readonly translate: RoundOutlineTranslator;
  private readonly rounds = new Map<string, RoundControls>();
  private readonly programBatches = new Map<string, ProgramBatchControls>();
  private readonly programMembers = new Map<string, ProgramBatchControls>();
  private readonly beforeBlocks = new Map<string, HTMLElement[]>();

  constructor(private readonly container: HTMLElement, translate?: RoundOutlineTranslator,
    private reveal?: (blockId: string) => void) {
    this.translate = translate ?? english;
  }

  /** Detached controls may still be held by focus/inspection code. They must
   * neither retain old tool payloads nor navigate a replacement Surface. */
  dispose(): void {
    this.reveal = undefined;
    for (const controls of this.rounds.values()) controls.blocks = [];
    this.actions.clear(); this.processEdges.clear(); this.rounds.clear();
    this.programBatches.clear(); this.programMembers.clear();
    this.beforeBlocks.clear();
  }

  private evidenceDisclosure(node: HTMLElement): HTMLDetailsElement | null {
    const details = node.querySelector<HTMLDetailsElement>('details');
    // A swarm panel is already the outer evidence card. Its first disclosure
    // contains task instructions, not this action's result; only its own
    // keyed controls may change internal task/history/error disclosure state.
    return details && !details.closest('.sw-panel') ? details : null;
  }

  update(turn: ConversationTurnViewModel, blocks: ReadonlyArray<GroupedTurnEntry>): void {
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
        if (block.kind === 'tool-panel' || !toolBlockOwnsSummary(block)) continue;
        const item = this.actions.get(block.blockId) ?? { state: 'returned' as const };
        item.state = toolBlockActivityState(block);
        next.set(block.blockId, item);
      }
    }
    this.actions.clear();
    for (const [id, item] of next) this.actions.set(id, item);
    this.updateRounds(turn, blocks);
  }

  private updateRounds(turn: ConversationTurnViewModel, mounted: ReadonlyArray<GroupedTurnEntry>): void {
    this.beforeBlocks.clear();
    const mountedIds = new Set(mounted.map(block => block.blockId));
    const byId = new Map(turn.blocks.map(block => [block.blockId, block]));
    const sections = selectRoundOutline(turn).filter(section => section.toolCount > 8);
    const retained = new Set(sections.map(section => section.id));
    for (const [id, controls] of this.rounds) {
      if (!retained.has(id)) { controls.header.remove(); this.rounds.delete(id); }
    }
    for (const section of sections) {
      const tools = section.blockIds.map(id => byId.get(id)!).filter(toolBlockOwnsSummary);
      let controls = this.rounds.get(section.id);
      if (!controls) { controls = this.createControls(section); this.rounds.set(section.id, controls); }
      controls.section = section; controls.blocks = tools;
      controls.label.textContent = `${this.translate('roundOutline.activity')} · ${
        this.translate('roundOutline.round', section.identity.llmRound + 1)} · ${
        this.translate('roundOutline.tools', section.toolCount)}`;
      controls.header.title = [section.identity.attemptId, section.identity.taskId].filter(Boolean).join(' · ');
      controls.search.setAttribute('aria-label', `${this.translate('roundOutline.search')} · ${
        this.translate('roundOutline.round', section.identity.llmRound + 1)}`);
      const first = tools.find(block => mountedIds.has(block.blockId));
      if (first) this.placeBefore(first.blockId, controls.header);
      else controls.header.remove();
      // Search DOM is reader-owned while streaming. Recompute only on input,
      // so an arriving result cannot replace the focused result button.
    }
    this.updateProgramBatches(turn, mountedIds);
  }

  private placeBefore(blockId: string, control: HTMLElement): void {
    const controls = this.beforeBlocks.get(blockId);
    if (controls) controls.push(control);
    else this.beforeBlocks.set(blockId, [control]);
  }

  /* Members stay mounted, keyed and in authored order; a fold only hides
   * them. The head sits immediately before the first mounted member. */
  private updateProgramBatches(turn: ConversationTurnViewModel, mountedIds: ReadonlySet<string>): void {
    const sections = selectProgramBatches(turn);
    const retained = new Set(sections.map(section => section.id));
    for (const [id, controls] of this.programBatches) {
      if (!retained.has(id)) { controls.head.remove(); this.programBatches.delete(id); }
    }
    this.programMembers.clear();
    for (const section of sections) {
      let controls = this.programBatches.get(section.id);
      if (!controls) { controls = this.createProgramBatch(section); this.programBatches.set(section.id, controls); }
      controls.section = section;
      controls.expanded = controls.choice ?? !section.summary.defaultCollapsed;
      this.paintProgramBatch(controls);
      for (const id of section.blockIds) this.programMembers.set(id, controls);
      const first = section.blockIds.find(id => mountedIds.has(id));
      if (first) this.placeBefore(first, controls.head);
      else controls.head.remove();
    }
  }

  private createProgramBatch(section: ProgramBatchSection): ProgramBatchControls {
    const doc = this.container.ownerDocument;
    const head = doc.createElement('button'); head.type = 'button';
    head.className = 'conversation-program-batch';
    const label = doc.createElement('span'); label.className = 'conversation-program-batch__label';
    head.append(label, disclosureChevron(doc, 'conversation-program-batch__chevron'));
    const controls: ProgramBatchControls = { section, head, label, expanded: false, labelKey: '' };
    head.addEventListener('click', () => {
      controls.choice = !controls.expanded;
      controls.expanded = controls.choice;
      this.paintProgramBatch(controls);
      const members = new Set(controls.section.blockIds);
      for (const node of this.container.querySelectorAll<HTMLElement>(':scope > [data-block-id]')) {
        if (members.has(node.dataset.blockId!)) {
          setData(node, 'programBatchFolded', controls.expanded ? undefined : 'true');
        }
      }
    });
    return controls;
  }

  private paintProgramBatch(controls: ProgramBatchControls): void {
    const { head, label, section } = controls;
    const parts = programBatchLabelParts(section.summary);
    const labelKey = JSON.stringify(parts);
    if (controls.labelKey !== labelKey) {
      controls.labelKey = labelKey;
      const doc = this.container.ownerDocument;
      label.replaceChildren(...parts.flatMap((part, index) => {
        const item = doc.createElement('span');
        if (part.status) item.dataset.programStatus = part.status;
        item.textContent = this.translate(part.key, part.count);
        return index ? [doc.createTextNode(' · '), item] : [item];
      }));
    }
    setData(head, 'attention', section.summary.attention);
    const expanded = String(controls.expanded);
    if (head.getAttribute('aria-expanded') !== expanded) head.setAttribute('aria-expanded', expanded);
    const title = [this.translate('roundOutline.round', section.identity.llmRound + 1),
      section.identity.attemptId, section.identity.taskId].filter(Boolean).join(' · ');
    if (head.title !== title) head.title = title;
    // Counts change as programs run; announce them like the tool panel label.
    if (section.summary.attention === 'active') {
      if (label.getAttribute('role') !== 'status') label.setAttribute('role', 'status');
    } else label.removeAttribute('role');
  }

  /** A search hit inside a folded batch expands it, as the reader would. */
  private unfoldProgramBatchOf(blockId: string): void {
    const controls = this.programMembers.get(blockId);
    if (controls && !controls.expanded) controls.head.click();
  }

  private createControls(section: RoundOutlineSection): RoundControls {
    const doc = this.container.ownerDocument;
    const header = doc.createElement('div'); header.className = 'conversation-round-header';
    const label = doc.createElement('span'); label.className = 'conversation-round-label';
    const search = doc.createElement('input'); search.type = 'search';
    search.className = 'conversation-round-search';
    search.placeholder = this.translate('roundOutline.search');
    search.setAttribute('aria-label', this.translate('roundOutline.search'));
    const results = doc.createElement('div'); results.className = 'conversation-round-matches'; results.hidden = true;
    const controls: RoundControls = { section, header, label, search, results, blocks: [], limit: 20 };
    header.append(label, search, results);
    search.addEventListener('input', () => { controls.limit = 20; this.search(controls); });
    search.addEventListener('keydown', event => {
      if (event.key === 'Escape') { search.value = ''; results.hidden = true; }
    });
    return controls;
  }

  private search(controls: RoundControls): void {
    const query = controls.search.value.trim().toLocaleLowerCase();
    controls.results.hidden = !query;
    controls.results.replaceChildren();
    if (!query) return;
    const matches = controls.blocks.filter(block => {
      const identity = toolSummaryIdentity(block);
      return `${identity.name} ${identity.target} ${identity.intent} ${block.blockId}`.toLocaleLowerCase().includes(query);
    });
    const doc = this.container.ownerDocument;
    const count = doc.createElement('span'); count.setAttribute('role', 'status');
    count.textContent = this.translate('roundOutline.matches', matches.length); controls.results.append(count);
    controls.limit = Math.max(20, Math.min(controls.limit, Math.ceil(matches.length / 20) * 20));
    for (const block of matches.slice(controls.limit - 20, controls.limit)) {
      const blockId = block.blockId;
      const identity = toolSummaryIdentity(block);
      const hit = doc.createElement('button'); hit.type = 'button';
      hit.textContent = `${identity.name} · ${identity.intent}${identity.target ? ` · ${identity.target}` : ''}`;
      hit.title = blockId;
      hit.addEventListener('click', () => {
        this.unfoldProgramBatchOf(blockId);
        this.reveal?.(blockId);
        const node = [...this.container.querySelectorAll<HTMLElement>(':scope > [data-block-id]')]
          .find(node => node.dataset.blockId === blockId);
        if (node) {
          node.tabIndex = -1;
          node.focus({ preventScroll: true }); node.scrollIntoView?.({ block: 'center', behavior: 'instant' });
        }
      });
      controls.results.append(hit);
    }
    if (controls.limit > 20) {
      const previous = doc.createElement('button'); previous.type = 'button';
      previous.textContent = this.translate('roundOutline.previousMatches');
      previous.addEventListener('click', () => { controls.limit -= 20; this.search(controls);
        controls.results.querySelector('button')?.focus(); });
      controls.results.append(previous);
    }
    if (matches.length > controls.limit) {
      const more = doc.createElement('button'); more.type = 'button';
      more.textContent = this.translate('roundOutline.moreMatches');
      more.addEventListener('click', () => { controls.limit += 20; this.search(controls);
        controls.results.querySelector('button')?.focus(); });
      controls.results.append(more);
    }
  }

  beforeBlock(blockId: string): readonly HTMLElement[] { return this.beforeBlocks.get(blockId) ?? []; }

  /* Every commit re-patches every mounted block, several times a second while
   * a reply streams; an unchanged value is never rewritten. */
  patchBlock(node: HTMLElement, block: ConversationBlockViewModel): void {
    const item = this.actions.get(block.blockId);
    const batch = this.programMembers.get(block.blockId);
    setData(node, 'deliverable', String(block.kind === 'text' && block.deliverable));
    setData(node, 'processEdge', this.processEdges.get(block.blockId));
    setData(node, 'toolSummary', item ? 'self-contained' : undefined);
    setData(node, 'actionState', item?.state);
    setData(node, 'programBatch', batch?.section.id);
    setData(node, 'programBatchFolded', batch && !batch.expanded ? 'true' : undefined);
  }

  completeBlock(node: HTMLElement, block: ConversationBlockViewModel): void {
    decorateToolSummary(node, block);
    const item = this.actions.get(block.blockId);
    if (!item) return;
    const batched = this.programMembers.has(block.blockId);
    if (!item.initialized) {
      // A program batch head summarizes its members, so none opens here; a
      // program card's presenter still reveals its own failure.
      const details = batched ? null : this.evidenceDisclosure(node);
      // Actionable evidence opens on first paint, except generic raw failures:
      // their concise diagnostic is already visible in the sole summary. Keep
      // raw envelopes folded; replays never override the reader's choice.
      if (details && item.state !== 'returned'
          && details.dataset.toolFailureEvidence !== 'collapsed') {
        details.open = true;
        item.revealed = details;
      }
      item.initialized = true;
    } else if (item.revealed && batched) {
      // A live round announces its programs one by one, so the first one
      // painted alone. Once siblings join, the batch head carries its
      // attention; a failure keeps its reveal.
      if (item.state !== 'attention') item.revealed.open = false;
      item.revealed = undefined;
    }
    // Generic results otherwise lack a verdict. Merge it into their sole row;
    // commands, approvals and rich warning cards already own explicit states.
    const summary = node.querySelector<HTMLElement>('[data-action-summary], .ptool-result-header');
    const prior = summary?.querySelector<HTMLElement>('.conversation-action-state');
    if (!summary || item.state === 'returned' || summary.querySelector(
      '.ptool-badge:not([hidden]), .ptool-cmd-status, .ptc-status, .hg-badge, .sw-status-pill, .conversation-tool-card__chip:not([hidden])')) {
      prior?.remove(); return;
    }
    const state = prior ?? node.ownerDocument.createElement('span');
    state.className = 'conversation-action-state';
    state.dataset.state = item.state;
    state.textContent = this.translate(`roundOutline.${item.state}`, 1);
    if (item.state === 'active') state.setAttribute('role', 'status');
    else state.removeAttribute('role');
    if (!prior) summary.append(state);
  }
}
