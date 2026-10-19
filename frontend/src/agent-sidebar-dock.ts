/** Right-hand background agent disclosure. Snapshot/Turn projections remain authority.
 * Owns only keyed DOM and reader folds; lazy execution detail uses the canonical
 * swarm renderer through ports. One render at a time; at most one queued
 * demand per mounted disclosure. No timers, transport or durable state.
 */
import { mountSwarmMessages, type SwarmMessagesSnapshot } from './conversation/ui/swarm-messages';
import type { Translator } from './i18n';
import { createButton } from './ui/button';

import { backgroundActivitySummary } from './background-activity-presentation';

import { resetManagedHtml, setManagedRichHtml } from './conversation/ui/renderers/stable-dom';

import { moveRetainedNodeBefore } from './conversation/ui/rich-card-reconciliation';
/** Same scope identifies turn/attempt/run/agent; empty task stamps are missing evidence. */
export interface AgentDockIdentity {
  scope: string;
  producerTask: string;
  workerTask: string;
}
export interface AgentDockItem {
  key: string;
  identity?: AgentDockIdentity;
  name: string;
  status: string;

  agentId?: string;
  swarmRunId?: string;

  statusLabel?: string;
  objective?: string;
  revision?: string;
  openConversation?: () => void;
  render(): Promise<string>;
}
interface AgentDockRow {
  key: string;
  identity?: AgentDockIdentity;
  generation: number;
  renderedGeneration?: number;
  el: HTMLDetailsElement;
  name: HTMLElement;
  status: HTMLElement;
  body: HTMLElement;
  conversationButton?: HTMLButtonElement;
  messages: HTMLDetailsElement;
  messageView?: ReturnType<typeof mountSwarmMessages>;
  loaded: boolean;
  renderedRevision?: string;
  source: AgentDockItem;
}
function compatibleIdentity(left: AgentDockIdentity, right: AgentDockIdentity): boolean {
  return left.scope === right.scope
    && (!left.producerTask || !right.producerTask || left.producerTask === right.producerTask)
    && (!left.workerTask || !right.workerTask || left.workerTask === right.workerTask);
}
const needsRender = (row: AgentDockRow) => !row.loaded || row.renderedGeneration !== row.generation
  || row.renderedRevision !== row.source.revision;
interface Ports {
  host: HTMLElement;
  translate(key: string, fallback: string, vars?: Record<string, string | number>): string;
}
const succeeded = (item: AgentDockItem) => ['done', 'completed', 'succeeded'].includes(item.status);
const archived = (item: AgentDockItem) => succeeded(item) || ['aborted', 'cancelled', 'canceled'].includes(item.status);
const PAGE_SIZE = 20;
export function createAgentSidebarDock({ host, translate: tt }: Ports) {
  const doc = host.ownerDocument;
  const el = doc.createElement('section');
  el.className = 'agent-dock';
  el.hidden = true;
  el.setAttribute('aria-label', tt('agentDock.title', 'Background agents'));
  const header = doc.createElement('header');
  const title = doc.createElement('strong');
  title.textContent = tt('agentDock.title', 'Background agents');
  el.dataset.conversationPart = 'background-agents';
  let mountedHost: HTMLElement | null = host;
  const heading = doc.createElement('div');
  heading.className = 'agent-dock-heading';
  const counts = doc.createElement('span');
  counts.className = 'agent-dock-counts';
  counts.setAttribute('aria-live', 'polite');
  heading.append(title, counts);
  header.append(heading);
  const active = doc.createElement('div');
  active.className = 'agent-dock-active';
  const history = doc.createElement('details');
  history.className = 'agent-dock-history';
  const historySummary = doc.createElement('summary');
  const historyBody = doc.createElement('div');
  const more = createButton({ label: tt('agentDock.more', 'Show more'), variant: 'ghost', size: 'sm', onClick: () => { limit += PAGE_SIZE; paint(); } }, doc);
  history.append(historySummary, historyBody, more);
  const messages = doc.createElement('details');
  messages.className = 'agent-dock-messages';
  const messagesSummary = doc.createElement('summary');
  messagesSummary.textContent = tt('agentDock.allMessages', 'All communications');
  messages.append(messagesSummary);
  const messageViews = new Map<string, ReturnType<typeof mountSwarmMessages>>();
  const messageTranslate = ((key: string, vars?: Record<string, string | number>) => tt(key, key, vars)) as Translator;
  el.append(header, active, history, messages);
  host.append(el);
  let disposed = false;

  let conversation = '';
  let epoch = 0;
  let items: readonly AgentDockItem[] = [];

  let runs: readonly SwarmMessagesSnapshot[] = [];
  let limit = PAGE_SIZE;
  let inFlight = false;
  const pending = new Set<AgentDockRow>();
  const rows = new Map<string, AgentDockRow>();
  el.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const disclosure = (event.target as Element)?.closest<HTMLDetailsElement>('.agent-dock-agent');
    if (!disclosure) return;
    disclosure.open = false;
    disclosure.querySelector<HTMLElement>('summary')?.focus();
    event.stopPropagation();
  });
  history.addEventListener('toggle', () => { if (!disposed) paint(); });
  function retireRow(row: AgentDockRow) {
    row.messageView?.dispose(); row.el.remove(); rows.delete(row.key); pending.delete(row);
  }
  function reconcileRows(next: readonly AgentDockItem[]) {
    const matches = new Map<AgentDockItem, AgentDockRow>();
    const retained = new Set<AgentDockRow>();
    // Exact keys take precedence, but cannot overrule an explicit identity conflict.
    for (const item of next) {
      const row = rows.get(item.key);
      if (row && (!row.identity || !item.identity || compatibleIdentity(row.identity, item.identity))) {
        matches.set(item, row); retained.add(row);
      }
    }
    const byScope = new Map<string, AgentDockRow[]>();
    for (const row of rows.values()) if (!retained.has(row) && row.identity) {
      const candidates = byScope.get(row.identity.scope) || [];
      candidates.push(row); byScope.set(row.identity.scope, candidates);
    }
    const uniqueCandidates = new Map<AgentDockItem, AgentDockRow>();
    const candidateCounts = new Map<AgentDockRow, number>();
    for (const item of next) if (!matches.has(item) && item.identity) {
      const candidates = (byScope.get(item.identity.scope) || [])
        .filter(row => compatibleIdentity(row.identity!, item.identity!));
      for (const row of candidates) candidateCounts.set(row, (candidateCounts.get(row) || 0) + 1);
      if (candidates.length === 1) uniqueCandidates.set(item, candidates[0]);
    }
    // Require uniqueness in BOTH directions; never choose by incoming order.
    for (const [item, row] of uniqueCandidates) if (candidateCounts.get(row) === 1) {
      matches.set(item, row); retained.add(row);
    }
    for (const row of rows.values()) if (!retained.has(row)) retireRow(row);
    rows.clear();
    for (const [item, row] of matches) {
      if (row.key !== item.key) {
        row.generation++;
        if (row.el.open) pending.add(row);
      }
      row.key = item.key; row.el.dataset.agentKey = item.key;
      // A sparse later snapshot must not erase a known conflict fence.
      if (item.identity) row.identity = { ...item.identity,
        producerTask: item.identity.producerTask || row.identity?.producerTask || '',
        workerTask: item.identity.workerTask || row.identity?.workerTask || '',
      };
      rows.set(item.key, row);
    }
  }
  async function drain() {
    if (inFlight || disposed || !mountedHost?.isConnected || pending.size === 0) return;
    const row = pending.values().next().value!;
    pending.delete(row);
    if (rows.get(row.key) !== row || !row.el.open || !needsRender(row)) { void drain(); return; }
    const capturedEpoch = epoch;
    const capturedGeneration = row.generation;
    const source = row.source;
    const canCommit = () => {
      if (disposed || !mountedHost?.isConnected || epoch !== capturedEpoch || rows.get(row.key) !== row || !row.el.open) return false;
      if (row.generation !== capturedGeneration || row.source.revision !== source.revision) {
        pending.add(row); return false;
      }
      return true;
    };
    inFlight = true;
    if (!row.loaded) row.body.textContent = tt('agentDock.loading', 'Loading execution details…');
    try {
      const detail = await source.render();
      if (!canCommit()) return;
      // The exact-run mailbox above owns communications when available; keep
      // legacy card messages only as a fallback when no run snapshot exists.
      setManagedRichHtml(row.body, detail);
      if (row.messageView) for (const node of row.body.querySelectorAll('.sw-a-messages')) node.remove();
      row.loaded = true;
      row.renderedRevision = source.revision;
      row.renderedGeneration = capturedGeneration;
    } catch {
      if (!canCommit()) return;
      resetManagedHtml(row.body);
      row.loaded = false;
      row.body.textContent = tt('agentDock.failed', 'Execution details could not be loaded.');
      row.body.append(createButton({ label: tt('agentDock.retry', 'Retry'), variant: 'ghost', size: 'sm', onClick: () => {
        if (disposed || rows.get(row.key) !== row || !row.el.open) return;
        pending.add(row); void drain();
      } }, doc));
    } finally { inFlight = false; void drain(); }
  }
  function paint() {
    if (disposed) return;
    const countLabel = backgroundActivitySummary({ agents: items }, tt);
    if (counts.textContent !== countLabel) counts.textContent = countLabel;
    const reading = new Set(items.filter(item => archived(item) && rows.get(item.key)?.el.open).map(item => item.key));
    const terminal = items.filter(item => archived(item) && !reading.has(item.key));
    const visible = [...items.filter(item => !archived(item) || reading.has(item.key)), ...(history.open ? terminal.slice(0, limit) : [])];
    const keep = new Set(visible.map(item => item.key));
    for (const [key, row] of rows) if (!keep.has(key)) retireRow(row);
    let activeIndex = 0;
    let historyIndex = 0;
    for (const item of visible) {
      let row = rows.get(item.key);
      if (!row) {
        const disclosure = doc.createElement('details');
        disclosure.className = 'agent-dock-agent';
        disclosure.dataset.agentKey = item.key;
        const summary = doc.createElement('summary');

        const name = doc.createElement('span');
        name.className = 'agent-dock-name';
        const status = doc.createElement('span');
        status.className = 'agent-dock-status';
        const body = doc.createElement('div');
        body.className = 'agent-dock-detail';
        summary.append(name, status);
        disclosure.append(summary);

        const communication = doc.createElement('details');
        communication.className = 'agent-dock-agent-messages';
        const communicationSummary = doc.createElement('summary');
        communicationSummary.textContent = tt('swarm.messagesTitle', 'Messages');
        communication.append(communicationSummary);
        row = { key: item.key, identity: item.identity && { ...item.identity }, generation: 0,
          el: disclosure, name, status, body, messages: communication, loaded: false, source: item };
        rows.set(item.key, row);
        const current = row;
        disclosure.append(communication, body);
        disclosure.addEventListener('toggle', () => {
          if (disposed || rows.get(current.key) !== current) return;
          if (!disclosure.open) { if (archived(current.source)) paint(); return; }
          if (needsRender(current)) { pending.add(current); void drain(); }
        });
      }
      if (row.source.revision !== item.revision && row.el.open) pending.add(row);
      row.source = item;

      if (item.openConversation && !row.conversationButton) {
        const current = row;
        const button = createButton({ label: tt('agentDock.openConversation', 'Open agent conversation'), variant: 'ghost', size: 'sm', onClick: () => {
          if (!disposed && rows.get(current.key) === current && current.conversationButton === button) current.source.openConversation?.();
        } }, doc);
        row.conversationButton = button;
        row.el.insertBefore(button, row.messages);
      } else if (!item.openConversation && row.conversationButton) {
        if (doc.activeElement === row.conversationButton) row.el.querySelector('summary')?.focus();
        row.conversationButton.remove(); row.conversationButton = undefined;
      }
      const run = item.swarmRunId && runs.find(candidate => candidate.swarm_id === item.swarmRunId);
      row.messages.hidden = !run || !item.agentId;
      if (run && item.agentId) {
        if (row.messageView) row.messageView.update(run, messageTranslate);
        else row.messageView = mountSwarmMessages(row.messages, run, messageTranslate, { participantId: item.agentId });
      } else { row.messageView?.dispose(); row.messageView = undefined; }
      row.el.dataset.status = item.status;

      if (row.name.textContent !== item.name) row.name.textContent = item.name;
      const phase = ({ done: 'complete', completed: 'complete', succeeded: 'complete', pending: 'queued', unknown: 'noResult' } as Record<string, string>)[item.status] || item.status;
      const label = item.statusLabel || tt('swarm.phase.' + phase, item.status);
      if (row.status.textContent !== label) row.status.textContent = label;
      row.el.querySelector('summary')!.setAttribute('aria-label', `${item.name} · ${label}`);
      row.el.title = item.objective || item.name;
      const inHistory = archived(item) && !reading.has(item.key);
      const target = inHistory ? historyBody : active;
      // Reorder only if necessary: moving a focused native disclosure loses focus.
      const index = inHistory ? historyIndex++ : activeIndex++;
      if (target.children[index] !== row.el) moveRetainedNodeBefore(target, row.el, target.children[index] || null);
    }
    historySummary.textContent = tt('agentDock.history', 'Finished history · {count}', { count: terminal.length });
    history.hidden = terminal.length === 0;
    more.hidden = terminal.length <= limit;
    el.hidden = items.length === 0 && messageViews.size === 0;


    void drain();
  }
  return {
    mount(next: HTMLElement | null) {
      if (disposed || (mountedHost === next && (!next || el.parentElement === next))) return;
      epoch++; pending.clear(); mountedHost = next;
      if (next) next.append(el); else el.remove();
      for (const row of rows.values()) if (row.el.open && needsRender(row)) pending.add(row);
      if (next) void drain();
    },
    update(conversationId: string, next: readonly AgentDockItem[], nextRuns: readonly SwarmMessagesSnapshot[] = []) {
      if (disposed) return;
      if (conversation !== conversationId) {
        epoch++; conversation = conversationId; pending.clear(); limit = PAGE_SIZE;
        for (const row of rows.values()) { row.messageView?.dispose(); row.el.remove(); }
        messages.open = false;
        rows.clear(); history.open = false;

        for (const view of messageViews.values()) view.dispose();
        messageViews.clear();
      }
      runs = nextRuns;
      messages.hidden = runs.length === 0;
      const keep = new Set(runs.map(run => run.swarm_id));
      for (const [run, view] of messageViews) if (!keep.has(run)) { view.dispose(); messageViews.delete(run); }
      for (const run of runs) {
        const view = messageViews.get(run.swarm_id);
        if (view) view.update(run, messageTranslate);
        else messageViews.set(run.swarm_id, mountSwarmMessages(messages, run, messageTranslate));
      }
      reconcileRows(next);
      items = next;
      paint();
    },
    dispose() {
      for (const row of rows.values()) row.messageView?.dispose();
      disposed = true; epoch++; pending.clear(); rows.clear(); items = [];

      for (const view of messageViews.values()) view.dispose();
      messageViews.clear();
      el.remove(); mountedHost = null;
    },
  };
}

/** The section is conversation activity, not a claim that the host Turn spawned it.
 * Use the selected main human Turn's existing context column/fold; never a new rail.
 * An unmounted latest Turn or foreign Surface is not permission to use old history.
 */
export function backgroundAgentContextHost(root: HTMLElement | null, conversationId: string, humanTurnId?: string): HTMLElement | null {
  if (!root || !conversationId) return null;
  const surface = root.matches('.conversation-surface') ? root : root.querySelector<HTMLElement>('.conversation-surface');
  if (surface?.dataset.conversationId !== conversationId) return null;
  const inputs = Array.from(surface.querySelectorAll<HTMLElement>('.user-msg[data-lane-id="main"][data-turn-id]'));
  const input = humanTurnId ? inputs.find(node => node.dataset.turnId === humanTurnId) : inputs.at(-1);
  if (!input) return null;
  const rail = Array.from(input.children).find(node => (node as HTMLElement).dataset.conversationPart === 'turn-context-rail') as HTMLElement | undefined;
  if (!rail) return null;
  if (surface.ownerDocument.defaultView?.getComputedStyle(rail).display !== 'none') return rail;
  return input.querySelector<HTMLElement>('.conversation-block--context');
}
