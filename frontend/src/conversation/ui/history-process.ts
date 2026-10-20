/** Surface-owned historical disclosure. No transport/store: immutable projected
 * blocks remain authoritative; only reader choices and a bounded DOM page live here.
 * Entry points: historicalTurnPlan and HistoricalProcess. */
import type { ConversationBlockViewModel as Block, ConversationTurnViewModel as Turn } from '../presentation/conversation-view-model';
import { toolBlockActivityState, roundIdentityOf } from '../presentation/round-outline';
import { hasAcceptedWorkUnit, toolRoundAttention } from '../presentation/tool-execution-groups';
import { t } from '../../i18n';
import { createButton } from '../../ui';

export interface HistoricalTurnPlan {
  readonly outside: readonly Block[];
  readonly process: readonly Block[];
  readonly fallback: boolean;
}
const plans = new WeakMap<Turn, HistoricalTurnPlan | null>();
const MAX_PROCESS_BLOCKS = 200;
const PROCESS_KINDS = new Set(['text', 'thinking', 'tool', 'program', 'compaction', 'injections', 'system-note', 'system-note-group', 'activity-event', 'activity-event-group']);
export function historicalTurnPlan(turn: Turn): HistoricalTurnPlan | null {
  if (plans.has(turn)) return plans.get(turn)!;
  let plan: HistoricalTurnPlan | null = null;
  if (turn.actor === 'assistant' && turn.role === 'assistant'
      && turn.status === 'completed' && !turn.commandPending
      && !turn.metadata.waitingOn) {
    // An unresolved/background/approval action overrides a settled Turn stamp.
    const unresolved = turn.blocks.some(block => {
      if (block.kind === 'live-status' || block.kind === 'proposed-plan') return true;
      if (block.kind !== 'tool' && block.kind !== 'program') return false;
      const attention = toolRoundAttention(block.round);
      const state = toolBlockActivityState(block);
      return attention === 'active' || attention === 'interactive'
        || state === 'active' || state === 'background';
    });
    if (!unresolved) {
      const final = [...turn.blocks].reverse().find(block => block.kind === 'text'
        && (block.terminal || block.deliverable) && block.markdown.trim());
      if (!final) {
        // Legacy/incomplete accepted projections have no reliable final boundary.
        // Keep all narration on the existing surface, explicitly explain why.
        plan = { outside: turn.blocks, process: [], fallback: true };
      } else {
        const finalIdentity = roundIdentityOf(final.source);
        const finalIndex = turn.blocks.indexOf(final);
        const isFinal = (block: Block, index: number): boolean => {
          if (block.kind !== 'text') return false;
          const identity = roundIdentityOf(block.source);
          return finalIdentity !== null
            ? identity?.key === finalIdentity.key
            : index === finalIndex;
        };
        const outside: Block[] = [], process: Block[] = [];
        turn.blocks.forEach((block, index) => {
          const work = (block.kind === 'tool' || block.kind === 'program') && hasAcceptedWorkUnit(block.round);
          if (work || isFinal(block, index) || !PROCESS_KINDS.has(block.kind)) outside.push(block);
          else process.push(block);
        });
        const finalBlocks = outside.filter(block => block.kind === 'text');
        plan = { outside: [...finalBlocks, ...outside.filter(block => block.kind !== 'text')], process, fallback: false };
      }
    }
  }
  plans.set(turn, plan);
  return plan;
}

interface LeafChoice { key: string; open?: boolean; scrollTop: number; expanded?: string | null; classes: string[]; }
interface ReaderState { open: boolean; start: number; scrollTop: number; focus: string | null; leaves: Map<string, LeafChoice[]>; }
const READER_CLASSES = ['is-open', 'is-collapsed', 'collapsed', 'open', 'expanded'];
// Authored identity where available; structural fallback never contains HTML.
function readerKey(node: HTMLElement, root: HTMLElement): string {
  const parts: string[] = [];
  for (let current: HTMLElement | null = node; current && current !== root; current = current.parentElement) {
    const identity = current.dataset.richKey || current.dataset.cmdKey || current.id;
    const index = current.parentElement ? Array.from(current.parentElement.children).indexOf(current) : 0;
    parts.push(`${current.tagName}:${identity || index}`);
  }
  return parts.reverse().join('/');
}
function remember(container: HTMLElement, state: ReaderState): void {
  for (const block of container.querySelectorAll<HTMLElement>('[data-block-id]')) {
    const choices: LeafChoice[] = [];
    for (const node of Array.from(block.querySelectorAll<HTMLElement>('details, [aria-expanded], [class], pre'))) {
      const classes = READER_CLASSES.filter(name => node.classList.contains(name));
      if (node.tagName !== 'DETAILS' && !node.hasAttribute('aria-expanded') && !node.scrollTop && !classes.length) continue;
      choices.push({ key: readerKey(node, block), open: node.tagName === 'DETAILS' ? (node as HTMLDetailsElement).open : undefined,
        scrollTop: node.scrollTop, expanded: node.getAttribute('aria-expanded'), classes });
      if (choices.length === 64) break;
    }
    state.leaves.set(block.dataset.blockId!, choices);
    const active = block.ownerDocument.activeElement as HTMLElement | null;
    if (active && block.contains(active)) state.focus = `${block.dataset.blockId}|${readerKey(active, block)}`;
  }
}
function restore(container: HTMLElement, state: ReaderState, restoreFocus: boolean): void {
  for (const block of container.querySelectorAll<HTMLElement>('[data-block-id]')) {
    const choices = new Map((state.leaves.get(block.dataset.blockId!) || []).map(choice => [choice.key, choice]));
    for (const node of Array.from(block.querySelectorAll<HTMLElement>('*'))) {
      const key = readerKey(node, block), choice = choices.get(key);
      if (choice) {
        if (choice.open !== undefined && node.tagName === 'DETAILS') (node as HTMLDetailsElement).open = choice.open;
        if (choice.expanded !== null && choice.expanded !== undefined) node.setAttribute('aria-expanded', choice.expanded);
        READER_CLASSES.forEach(name => node.classList.toggle(name, choice.classes.includes(name)));
        node.scrollTop = choice.scrollTop;
      }
      if (restoreFocus && state.focus === `${block.dataset.blockId}|${key}`) node.focus({ preventScroll: true });
    }
  }
}

/** Retained controller; pages replace leaves, never issue outer window intents.
 * UI snapshots have no projection/result copies. Disposal calls the renderer's
 * cancellation boundary before detaching nodes. */
export class HistoricalProcess {
  private states = new Map<string, ReaderState>();
  private mounted = new Map<HTMLElement, { release: () => void; update: (plan: HistoricalTurnPlan, budget: number) => void }>();
  private closeInspector: (() => void) | null = null;
  constructor(private readonly remove: (node: HTMLElement) => void,
    private readonly render: (node: HTMLElement, blocks: readonly Block[]) => void,
    private readonly suspendFollow: () => void) {}
  update(host: HTMLElement, turn: Turn, plan: HistoricalTurnPlan | null, budget: number): void {
    if (!plan || plan.fallback || !plan.process.length) {
      this.mounted.get(host)?.release(); host.replaceChildren(); return;
    }
    const mounted = this.mounted.get(host);
    if (mounted) { mounted.update(plan, budget); return; }
    let currentPlan = plan, currentBudget = Math.min(MAX_PROCESS_BLOCKS, budget);
    const key = turn.presentationId || turn.turnId;
    let state = this.states.get(key);
    if (!state) { state = { open: false, start: 0, scrollTop: 0, focus: null, leaves: new Map() }; this.states.set(key, state); }
    const reader = state;
    let details = host.querySelector<HTMLDetailsElement>(':scope > details');
    if (!details) { details = host.ownerDocument.createElement('details'); details.className = 'conversation-history-process'; host.append(details); }
    details.dataset.historyProcess = key;
    let summary = details.querySelector<HTMLElement>(':scope > summary');
    if (!summary) { summary = host.ownerDocument.createElement('summary'); details.prepend(summary); }
    summary.textContent = t('conversation.historyProcess', { n: currentPlan.process.length });
    const disclosure = details;
    let body: HTMLElement | null = null;
    const release = () => {
      if (this.closeInspector === close) this.closeInspector = null;
      if (!body) return;
      reader.scrollTop = body.scrollTop;
      remember(body, reader);
      this.remove(body); body = null;
    };
    const close = (restoreFocus = false) => {
      if (restoreFocus) summary!.focus();
      reader.open = false; disclosure.open = false; release();
      if (this.closeInspector === close) this.closeInspector = null;
    };
    const paint = (navigation = false) => {
      if (!disclosure.open) { release(); return; }
      const restoreFocus = !body || body.contains(host.ownerDocument.activeElement);
      if (body) { reader.scrollTop = navigation ? 0 : body.scrollTop; remember(body, reader); }
      if (navigation) reader.focus = null;
      if (!body) {
        this.closeInspector?.(); this.closeInspector = close;
        body = host.ownerDocument.createElement('section');
        body.className = 'conversation-history-process__body';
        body.setAttribute('role', 'region');
        body.setAttribute('aria-label', t('conversation.processInspector'));
        const bar = host.ownerDocument.createElement('header');
        bar.className = 'conversation-history-process__heading';
        const title = host.ownerDocument.createElement('strong');
        title.textContent = t('conversation.processInspector');
        bar.append(title, createButton({ label: t('conversation.backToAnswer'),
          variant: 'ghost', size: 'sm', onClick: () => close(true) }, host.ownerDocument));
        body.append(bar);
        body.addEventListener('keydown', event => {
          if (event.key !== 'Escape' || event.defaultPrevented) return;
          event.preventDefault(); event.stopPropagation(); close(true);
        });
        disclosure.append(body);
        const pane = body;
        // Native top layer preserves ancestry/delegation and escapes chat's
        // content-visibility/scroll containment without moving live evidence.
        // A non-modal inspector leaves the answer usable. Older engines keep
        // the same disclosure inline; disposal invalidates the queued mount.
        if (typeof pane.showPopover === 'function') {
          pane.setAttribute('popover', 'manual');
          queueMicrotask(() => {
            if (body === pane && pane.isConnected && disclosure.open
                && !pane.matches(':popover-open')) {
              pane.showPopover();
              if (host.ownerDocument.activeElement === summary) {
                pane.querySelector<HTMLButtonElement>('.conversation-history-process__heading > button')?.focus({ preventScroll: true });
              }
            }
          });
        }
      }
      body.setAttribute('aria-label', t('conversation.processInspector'));
      body.querySelector('.conversation-history-process__heading > strong')!.textContent = t('conversation.processInspector');
      body.querySelector('.conversation-history-process__heading > button')!.textContent = t('conversation.backToAnswer');
      reader.start = Math.max(0, Math.min(reader.start, Math.max(0, currentPlan.process.length - 1)));
      const end = Math.min(currentPlan.process.length, reader.start + currentBudget);
      let controls = body.querySelector<HTMLElement>(':scope > nav');
      if (!controls) { controls = host.ownerDocument.createElement('nav'); body.querySelector('header')!.after(controls); }
      controls.className = 'conversation-history-process__controls';
      controls.setAttribute('aria-label', t('conversation.historyProcessPages'));
      // A complete page needs no disabled pagination chrome. Keep the same
      // keyed controls for a later budget change or a retained nonzero page.
      controls.hidden = reader.start === 0 && end === currentPlan.process.length;
      if (controls.hidden && controls.contains(host.ownerDocument.activeElement)) summary!.focus({ preventScroll: true });

      const button = (action: string, label: string, start: number, disabled: boolean) => {
        let node = controls!.querySelector<HTMLButtonElement>(`[data-process-page="${action}"]`);
        if (!node) { node = createButton({ label, variant: 'ghost', size: 'sm', wrap: true }, host.ownerDocument); node.dataset.processPage = action; controls!.append(node); }
        node.textContent = label; node.disabled = disabled;
        node.onclick = () => { this.suspendFollow(); reader.start = start; reader.scrollTop = 0; reader.focus = null; paint(true); node!.blur(); controls?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus({ preventScroll: true }); };
      };
      button('previous', t('conversation.historyProcessPrevious'), Math.max(0, reader.start - currentBudget), reader.start === 0);
      let range = controls.querySelector('span');
      if (!range) { range = host.ownerDocument.createElement('span'); controls.append(range); }
      range.textContent = `${reader.start + 1}–${end} / ${currentPlan.process.length}`; range.setAttribute('aria-live', 'polite');
      button('next', t('conversation.historyProcessNext'), reader.start + currentBudget, end === currentPlan.process.length);
      let blocks = body.querySelector<HTMLElement>(':scope > [data-history-process-blocks]');
      if (!blocks) { blocks = host.ownerDocument.createElement('div'); blocks.dataset.historyProcessBlocks = ''; body.append(blocks); }
      // Process bypasses action outline rearrangement: preserve authored chronology.
      this.render(blocks, currentPlan.process.slice(reader.start, end));
      restore(blocks, reader, restoreFocus && !navigation); body.scrollTop = reader.scrollTop;
    };
    const toggle = (event: Event) => {
      if (event.target !== disclosure) return;
      reader.open = disclosure.open; this.suspendFollow(); paint();
    };
    disclosure.addEventListener('toggle', toggle);
    // Native interaction closes synchronously: cancel reads before the async toggle event.
    const click = (event: Event) => { if (disclosure.open && (event.target as Element).closest('summary') === summary) release(); };
    summary.addEventListener('click', click);
    disclosure.open = reader.open;
    paint();
    this.mounted.set(host, {
      release: () => { release(); if (this.closeInspector === close) this.closeInspector = null;
        disclosure.removeEventListener('toggle', toggle); summary!.removeEventListener('click', click); this.mounted.delete(host); },
      update: (next, nextBudget) => { currentPlan = next; currentBudget = Math.min(MAX_PROCESS_BLOCKS, nextBudget);
        summary!.textContent = t('conversation.historyProcess', { n: next.process.length }); paint(); },
    });
  }
  unmount(node: HTMLElement): void {
    for (const [host, release] of this.mounted) if (node === host || node.contains(host)) release.release();
  }
  reset(): void { for (const mounted of [...this.mounted.values()]) mounted.release(); this.states.clear(); }
}
