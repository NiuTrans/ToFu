/** Flat Inspector feed projection and DOM. Entry: renderInspectorFeed.
 * These local shapes describe the fields this view reads; wire acceptance and
 * last-good settlement stay in diagnostics-wire-controller. Message reconciliation,
 * native controls and pane notices retain their common owners. No retained DOM.
 */
import {createButton} from '../ui';
import type {RenderOptions} from './diagnostics-message-reconciliation';
import {inspectorNotice, inspectorRetry, type InspectorContent, type InspectorTranslate} from './inspector-pane-view';

export interface InspectorFeedTurn {
  turnId?: string; taskId?: string; status?: string; live?: boolean;
  preview?: string; createdAt?: number; expired?: boolean; readError?: boolean;
}
interface Section extends InspectorFeedTurn {
  type?: string; msgIndex?: number; kept?: number; dropped?: number; agentId?: string;
}
export interface InspectorFeedDocument {
  messages: readonly unknown[]; sections: readonly Section[]; turns: readonly InspectorFeedTurn[];
  eventReadBounds?: readonly unknown[]; olderTurnsDropped?: number;
}
export interface InspectorFeedViewPorts {
  translate: InspectorTranslate;
  reconcile(content: HTMLElement, messages: readonly unknown[], options: RenderOptions): void;
  ordinal(turn?: string): number | null;
  relativeTime(time?: number): string;
  absoluteTime(time?: number): string;
}
interface Actions { retry(): void; showTurn(turn: string): void; trace(task: string): void }

function status(row: InspectorFeedTurn, t: InspectorTranslate): {tone: string; label: string} {
  const raw = String(row.status || '').toLowerCase();
  if (row.live || ['running', 'queued', 'pending'].includes(raw)) return {tone: 'running', label: t('ri.statusRunning')};
  if (['done', 'completed', 'complete', 'success'].includes(raw)) return {tone: 'done', label: t('ri.statusDone')};
  if (['error', 'failed', 'failure'].includes(raw)) return {tone: 'error', label: t('ri.statusFailed')};
  if (['aborted', 'interrupted', 'cancelled', 'canceled', 'stopped'].includes(raw)) return {tone: 'stopped', label: t('ri.statusStopped')};
  return {tone: 'neutral', label: raw || t('ri.statusUnknown')};
}

function divider(section: Section, ports: InspectorFeedViewPorts, actions: Actions): HTMLElement {
  const t = ports.translate;
  if (section.type === 'rebuild') return inspectorNotice('ri-feed-rebuild',
    t('ri.rebuildNote', {k: section.kept || 0, d: section.dropped || 0}));
  if (section.type === 'agent') {
    const node = inspectorNotice('ri-feed-agent', '');
    const badge = document.createElement('span'); badge.className = 'ri-agent-badge'; badge.textContent = section.agentId || 'agent';
    const label = document.createElement('span'); label.className = 'ri-feed-agent-label'; label.textContent = t('ri.agentSection');
    node.append(badge, label); return node;
  }
  const node = inspectorNotice('ri-feed-turn', '');
  node.dataset.turnId = section.turnId || ''; node.dataset.taskId = section.taskId || '';
  const ordinal = ports.ordinal(section.turnId);
  const chip = createButton({label: ordinal ? t('ri.turnChip', {n: ordinal}) : t('ri.earlierShort'),
    title: t('ri.turnChipTip'), variant: 'ghost', size: 'sm',
    onClick: () => { if (section.turnId) actions.showTurn(section.turnId); }});
  chip.classList.add('ri-turn-chip');
  if (!ordinal) chip.classList.add('ri-turn-chip-muted');
  const state = status(section, t), badge = document.createElement('span');
  badge.className = 'ri-task-status ri-tone-' + state.tone;
  const dot = document.createElement('span'); dot.className = 'ri-task-status-dot'; dot.setAttribute('aria-hidden', 'true');
  badge.append(dot, state.label);
  const preview = document.createElement('span'); preview.className = 'ri-feed-turn-preview';
  preview.textContent = section.readError ? t('ri.loadFailed') : section.expired ? t('ri.expired') : (section.preview || '');
  preview.title = preview.textContent;
  const time = document.createElement('span'); time.className = 'ri-feed-turn-time';
  time.textContent = ports.relativeTime(section.createdAt); time.title = ports.absoluteTime(section.createdAt);
  node.append(chip, badge, preview, time);
  if (section.taskId) {
    const trace = createButton({label: t('ri.traceEntry'), title: t('ri.traceEntryHint'), variant: 'ghost', size: 'sm',
      onClick: () => actions.trace(section.taskId!)});
    trace.classList.add('ri-feed-trace'); node.append(trace);
  }
  return node;
}

export function renderInspectorFeed(content: InspectorContent, title: HTMLElement | null, scroller: HTMLElement | null,
  wire: InspectorFeedDocument, readError: boolean, keepScroll: boolean, ports: InspectorFeedViewPorts, actions: Actions): void {
  const t = ports.translate, {messages, sections, turns} = wire;
  const previousScroll = keepScroll ? scroller?.scrollTop || 0 : 0;
  const atBottom = !!(keepScroll && scroller && scroller.scrollHeight > 0
    && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 40);
  if (title) title.textContent = `Messages (${messages.length})` + (turns.length ? ` · ${t('ri.feedTurns', {n: turns.length})}` : '');
  const leading: HTMLElement[] = [];
  if (readError) {
    const warning = inspectorNotice('ri-warn-line', t('ri.loadFailed'));
    warning.append(inspectorRetry(t, actions.retry)); leading.push(warning);
  }
  if (Array.isArray(wire.eventReadBounds) && wire.eventReadBounds.length) leading.push(inspectorNotice('ri-feed-truncated', t('ri.eventWindowPartial')));
  if (wire.olderTurnsDropped) leading.push(inspectorNotice('ri-feed-truncated', t('ri.feedTruncated', {n: wire.olderTurnsDropped})));
  const beforeMessage = new Map<number, Node[]>();
  for (const section of sections) {
    const index = Math.max(0, Math.min(messages.length, Number(section.msgIndex) || 0));
    if (!beforeMessage.has(index)) beforeMessage.set(index, []);
    beforeMessage.get(index)!.push(divider(section, ports, actions));
  }
  const empty = !messages.length && !sections.length;
  if (empty) leading.push(inspectorNotice('ri-main-empty', t('ri.empty')));
  ports.reconcile(content, messages, {reuse: keepScroll && !empty ? 'identity' : false, leading, beforeMessage});
  content._rawMessages = messages; content._rawTools = null;
  if (keepScroll && scroller && !empty) scroller.scrollTop = atBottom ? scroller.scrollHeight : previousScroll;
}
