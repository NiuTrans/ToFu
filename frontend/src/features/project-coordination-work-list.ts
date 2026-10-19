/** Compact, read-only work list. Task evidence and conversation shells remain
 * different kinds: an empty conversation is never a queued assignment. Graph
 * projection owns execution/dependency facts; pages own bounded DOM demand.
 */
import { createButton, createEmptyState } from '../ui';
import { t } from '../i18n';
import type { CoordinationGraph, CoordinationGraphNode } from './project-coordination-graph';
import { createCoordinationReference, captureCoordinationView } from './project-coordination-reference';

export interface CoordinationConversation { id: string; title: string; turnCount?: number; coordinatorId?: string | null; }
export interface CoordinationListState { workPage?: number; }
export interface CoordinationListRow {
  id: string; title: string; conversationId?: string;
  status: string; task?: CoordinationGraphNode;
}
export const COORDINATION_LIST_PAGE_SIZE = 8;

export function projectCoordinationList(graph: CoordinationGraph, conversations: readonly CoordinationConversation[] = []): CoordinationListRow[] {
  const rows = graph.nodes.filter(node => node.kind === 'task').map(node => ({
    id: node.id, title: node.title, conversationId: node.conversationId, status: node.status || 'unknown', task: node,
  }));
  const represented = new Set(graph.nodes.map(node => node.conversationId));
  const shells: CoordinationListRow[] = [];
  for (const item of conversations) {
    if (represented.has(item.id)) continue;
    represented.add(item.id);
    shells.push({ id: `conversation:${item.id}`, title: item.title || item.id, conversationId: item.id,
      status: item.turnCount === 0 ? 'not_started' : item.turnCount === undefined ? 'unknown' : 'conversation' });
  }
  // Attention/current work comes before returned work, with stable source order.
  return [...rows.filter(row => !['completed', 'cancelled'].includes(row.status)), ...shells,
    ...rows.filter(row => ['completed', 'cancelled'].includes(row.status))];
}

export function createCoordinationWorkList(graph: CoordinationGraph, navigate: (id: string) => void, options: {
  conversations?: readonly CoordinationConversation[];
  state: CoordinationListState;
  digest(node: CoordinationGraphNode): string;
  inspect?(id: string): void;
  manage?(): void;
  /** Team shows one row per conversation, including assignment-only recipients. */
  team?: boolean;
}): HTMLElement {
  let rows = projectCoordinationList(graph, options.conversations);
  if (options.team) {
    const byConversation = new Map<string, CoordinationListRow>();
    for (const item of options.conversations || []) {
      const task = rows.find(row => row.conversationId === item.id);
      if (task) byConversation.set(item.id, { ...task, title: item.title || task.title });
    }
    for (const row of rows) if (row.conversationId && !byConversation.has(row.conversationId)) byConversation.set(row.conversationId, row);
    rows = [...byConversation.values()];
  }
  const root = document.createElement('section'); root.className = 'coordination-work-list';
  root.setAttribute('aria-label', t(options.team ? 'projectCoordination.team' : 'projectCoordination.workList'));
  function render() {
    const restore = captureCoordinationView(root);
    const page = Math.max(0, Math.min(options.state.workPage || 0, Math.max(0, Math.ceil(rows.length / COORDINATION_LIST_PAGE_SIZE) - 1)));
    options.state.workPage = page;
    root.replaceChildren();
    const list = document.createElement('div'); list.className = 'coordination-work-rows';
    list.dataset.coordinationScroll = options.team ? 'team-list' : 'work-list';
    for (const row of rows.slice(page * COORDINATION_LIST_PAGE_SIZE, (page + 1) * COORDINATION_LIST_PAGE_SIZE)) {
      const card = document.createElement('article'); card.className = 'coordination-work-row';
      card.dataset.workId = row.id; card.dataset.status = row.status;

      const viewKey = options.team ? `team:${row.id}` : row.id;
      const body = document.createElement('div'); body.className = 'coordination-work-body';
      const title = document.createElement('strong'); title.textContent = row.title;
      const status = document.createElement('small'); status.className = 'coordination-work-status';
      status.textContent = row.task ? t(`projectCoordination.graph.status.${row.task.status || 'unknown'}`)
        : t(row.status === 'not_started' ? 'projectCoordination.shell.not_started'
          : row.status === 'conversation' ? 'projectCoordination.shell.conversation' : 'projectCoordination.shell.unknown');
      body.append(title, status);
      if (row.task) {
        const digest = options.digest(row.task);
        if (digest) { const summary = document.createElement('p'); summary.className = 'coordination-work-digest'; summary.textContent = digest; body.append(summary); }
        if (row.task.delivered !== undefined) {
          const delivery = document.createElement('small'); delivery.textContent = t(row.task.delivered
            ? 'projectCoordination.graph.deliveryConfirmed' : 'projectCoordination.graph.deliveryUnconfirmed'); body.append(delivery);
        }
        for (const edge of graph.edges.filter(edge => edge.to === row.id && edge.kind === 'dependency')) {
          const dependency = document.createElement('small'); dependency.textContent = t(`projectCoordination.graph.dependency.${edge.status || 'unknown'}`);
          body.append(dependency);
        }
        if (row.task.revoked) { const revoked = document.createElement('small'); revoked.textContent = t('projectCoordination.graph.revoked'); body.append(revoked); }
      }
      const actions = document.createElement('div'); actions.className = 'coordination-work-actions';
      if (row.conversationId) {
        const open = createButton({ label: t('projectCoordination.openWorker'), size: 'sm', variant: 'ghost', wrap: true,
          onClick: () => navigate(row.conversationId!) });
        open.dataset.coordinationFocus = `work-open:${viewKey}`; actions.append(open);
      }
      if (row.task && options.manage && ['failed', 'blocked', 'waiting', 'unknown', 'completed'].includes(row.status)) {
        const manage = createButton({ label: t('projectCoordination.controls'), size: 'sm', variant: 'ghost', onClick: options.manage });
        manage.dataset.coordinationFocus = `work-manage:${viewKey}`; actions.append(manage);
      }
      actions.append(createCoordinationReference(row.task?.reference || { conversationId: row.conversationId }, viewKey, options.inspect));
      card.append(body, actions); list.append(card);
    }
    root.append(list);
    if (!rows.length) root.append(createEmptyState({ title: t(graph.total === null ? 'projectCoordination.graph.progressUnknown' : 'projectCoordination.graph.empty') }));
    if (rows.length > COORDINATION_LIST_PAGE_SIZE) {
      const paging = document.createElement('div'); paging.className = 'coordination-list-paging';
      const previous = createButton({ label: t('projectCoordination.list.previous'), size: 'sm', variant: 'ghost', onClick: () => { options.state.workPage = page - 1; render(); } });
      previous.disabled = page === 0; previous.dataset.coordinationFocus = options.team ? 'team-list-previous' : 'list-previous';
      const next = createButton({ label: t('projectCoordination.list.more'), size: 'sm', variant: 'ghost', onClick: () => { options.state.workPage = page + 1; render(); } });
      next.disabled = (page + 1) * COORDINATION_LIST_PAGE_SIZE >= rows.length; next.dataset.coordinationFocus = options.team ? 'team-list-more' : 'list-more';
      const count = document.createElement('small'); count.textContent = t('projectCoordination.list.page', {
        start: page * COORDINATION_LIST_PAGE_SIZE + 1, end: Math.min((page + 1) * COORDINATION_LIST_PAGE_SIZE, rows.length), total: rows.length });
      paging.append(previous, count, next); root.append(paging);
    }
    restore();
  }
  render(); return root;
}
