/** Conversation-first project navigation. Server binding alone confers the role;
 * board/assignment evidence alone identifies related work. No polling or writes.
 */
import type { CoordinationCanvasState } from './project-coordination-canvas';
import { createButton } from '../ui';
import { createCoordinatorBudget } from './coordinator-budget';
import { createAssignmentHistoryNavigation } from './project-assignment-history';
import { t } from '../i18n';

import { createCoordinatorReport, coordinatorReportLabel } from './project-coordinator-report';
import type { CoordinatorReportState } from '../api/project-coordination.generated';
import { projectCoordinationGraph } from './project-coordination-graph';
import { coordinationDecisions, coordinationText, createCoordinationBoard, projectCoordinationWorkGroups } from './project-coordination-board';
import type { ProjectCoordinationPorts } from './project-coordination';
import type { CoordinationBinding } from '../api/project-coordination.generated';
import '../styles/application/33-project-coordination.css';

/** Hub-with-satellites mark: the sidebar entry needed a recognizable identity,
 * not a bare text row that read as a stray label. */
function coordinationMark(): SVGSVGElement {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
  const node = (cx: string, cy: string, r: string) => {
    const circle = document.createElementNS(ns, 'circle');
    circle.setAttribute('cx', cx); circle.setAttribute('cy', cy); circle.setAttribute('r', r);
    circle.setAttribute('fill', 'currentColor'); return circle;
  };
  for (const [x, y] of [['8', '3'], ['3', '12'], ['13', '12']]) {
    const spoke = document.createElementNS(ns, 'line');
    spoke.setAttribute('x1', '8'); spoke.setAttribute('y1', '8'); spoke.setAttribute('x2', x); spoke.setAttribute('y2', y);
    spoke.setAttribute('stroke', 'currentColor'); spoke.setAttribute('stroke-width', '1');
    spoke.setAttribute('opacity', '0.45'); svg.append(spoke);
  }
  svg.append(node('8', '8', '2.6'), node('8', '3', '1.4'), node('3', '12', '1.4'), node('13', '12', '1.4'));
  return svg;
}

export function createProjectCoordinationSurface(ports: {
  sidebar: HTMLElement; canvas: HTMLElement;

  mode?: 'sidebar' | 'canvas';
  scope(): ProjectCoordinationPorts | null;
  sidebarPath(): string;
  details(scope: ProjectCoordinationPorts): void;
  create(scope: ProjectCoordinationPorts, binding: CoordinationBinding): Promise<void> | void;

  canCreate?(): boolean;

  changed?(): void;
}) {
  let alive = true, generation = 0, flight = false, pending = false;
  let signature = '', projectSignature = '', expanded = true;

  let rendered = '';
  let creating = false, reportExpanded = false;
  let historyCursor: string | undefined;
  let lastReport: CoordinatorReportState | undefined;
  // Preserve graph selection, zoom and history within the mounted project.
  const graphState: CoordinationCanvasState = {};
  let unsubscribe: (() => void) | undefined;
  let subscribedPath = '';
  const norm = (path: string) => path.replace(/[/\\]+$/, '');
  function invalidate() { pending = true; void refresh(true); }
  async function refresh(force = false) {
    if (!alive) return;
    const scope = ports.scope();
    const key = JSON.stringify([scope?.path, scope?.currentConversationId, ports.sidebarPath()]);

    if (!force && key === signature) return;
    if (key !== signature) {
      /* Only a project/folder switch (or unmount) tears the surfaces down; a
       * conversation switch inside one project must leave the coordinator row
       * mounted so the sidebar does not jump. */
      const projectKey = JSON.stringify([scope?.path, ports.sidebarPath()]);
      const projectChanged = projectKey !== projectSignature;
      projectSignature = projectKey;
      signature = key; generation++;
      if (projectChanged) {
        ports.sidebar.replaceChildren(); ports.canvas.replaceChildren(); expanded = true; rendered = '';
        delete graphState.selectedId; delete graphState.showHistory; delete graphState.zoom;
        lastReport = undefined; reportExpanded = false;
        historyCursor = undefined;
      } else {
        // The previous conversation's graph must not remain in the new canvas
        // while its scope read is in flight. Sidebar geometry stays stable.
        expanded = ports.canvas.querySelector<HTMLDetailsElement>('.coordination-summary')?.open ?? expanded;
        ports.canvas.replaceChildren();
      }
    }
    if (!scope) { unsubscribe?.(); unsubscribe = undefined; subscribedPath = ''; return; }
    if (subscribedPath !== scope.path) {
      unsubscribe?.(); subscribedPath = scope.path;
      unsubscribe = scope.subscribe?.(invalidate);
    }
    if (flight) { pending = true; return; }
    flight = true; pending = false;
    const own = generation;
    const valid = () => alive && own === generation && norm(scope.currentPath()) === norm(scope.path)
      && (!scope.currentConversation || scope.currentConversation() === scope.currentConversationId);
    try {
      const [bindingRead, boardRead, assignmentsRead, reportRead, budgetRead] = await Promise.allSettled([
        scope.binding(scope.path), scope.board(scope.path),
        scope.dispatch ? scope.dispatch.list(scope.path, undefined, historyCursor ? { cursor: historyCursor } : undefined)
          : Promise.resolve({ assignments: [], olderCursor: null, newerCursor: null }),
        scope.reports ? scope.reports.read(scope.path) : Promise.resolve(undefined),
        ports.mode !== 'sidebar' && scope.budget ? scope.budget.read(scope.path) : Promise.resolve(null),
      ]);
      if (!valid()) return;
      const binding: CoordinationBinding | null = bindingRead.status === 'fulfilled' ? bindingRead.value : null;
      const board = boardRead.status === 'fulfilled' ? boardRead.value : null;
      const assignmentPage = assignmentsRead.status === 'fulfilled' ? assignmentsRead.value : null;
      const assignments = assignmentPage?.assignments ?? null;
      const budget = budgetRead.status === 'fulfilled' ? budgetRead.value : null;

      const reportUnavailable = reportRead.status === 'rejected';
      if (reportRead.status === 'fulfilled') lastReport = reportRead.value;
      const report = lastReport?.conversationId === binding?.conversationId
        && lastReport?.bindingRevision === binding?.revision ? lastReport : undefined;
      const groups = projectCoordinationWorkGroups(board?.active || [], board?.recentOutcomes || []);
      const graphOptions = { assignments, coordinatorId: binding?.conversationId, activeConversationId: scope.currentConversationId };
      const graph = projectCoordinationGraph(groups, graphOptions);
      const facts = JSON.stringify([key, binding, board, graph, report, reportUnavailable, budget,
        historyCursor, assignmentPage?.olderCursor, assignmentPage?.newerCursor,
        assignments && coordinationDecisions(assignments),
        assignments?.some(item => !item.revoked && item.request.recipientId === scope.currentConversationId)]);
      if (facts === rendered) return;
      rendered = facts;
      expanded = ports.canvas.querySelector<HTMLDetailsElement>('.coordination-summary')?.open ?? expanded;
      const focused = document.activeElement;
      const focusHost = focused && ports.sidebar.contains(focused) ? ports.sidebar
        : focused && ports.canvas.contains(focused) ? ports.canvas : null;
      const focusLabel = focusHost ? focused?.textContent : null;

      const focusKey = focusHost && focused instanceof HTMLElement ? focused.dataset.coordinationFocus : undefined;
      const viewport = ports.canvas.querySelector('.coordination-graph-viewport');
      const graphScroll = viewport ? [viewport.scrollLeft, viewport.scrollTop] : null;
      const details = () => { if (valid()) ports.details(scope); };
      const navigate = (id: string) => { if (valid()) scope.navigate(id); };

      const decisions = assignments && coordinationDecisions(assignments);
      ports.sidebar.replaceChildren(); ports.canvas.replaceChildren();
      if (ports.mode !== 'canvas' && norm(ports.sidebarPath()) === norm(scope.path)) {
        const available = binding?.availability === 'available';
        const row = document.createElement('section'); row.className = 'coordination-leader';
        row.dataset.coordinatorId = available ? binding!.conversationId : '';
        if (available) {
          row.tabIndex = 0; row.setAttribute('role', 'button');
          row.title = t('projectCoordination.openCoordinator');
          const open = () => navigate(binding!.conversationId);
          row.addEventListener('click', open);
          row.addEventListener('keydown', event => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault(); open();
          });
          if (binding!.conversationId === scope.currentConversationId) row.setAttribute('aria-current', 'page');
        }
        // The mark and the work tally explain what this fixed row is; a bare
        // name read as a stray label above the date groups.
        const mark = document.createElement('span'); mark.className = 'coordination-leader-mark';
        mark.setAttribute('aria-hidden', 'true'); mark.append(coordinationMark());
        const body = document.createElement('span'); body.className = 'coordination-leader-body';
        const leader = document.createElement('span'); leader.className = 'coordination-leader-label';
        leader.textContent = t('projectCoordination.coordinator'); body.append(leader);
        const meta = document.createElement('span'); meta.className = 'coordination-leader-meta';
        if (available) {
          const active = graph.counts.running + graph.counts.queued + graph.counts.blocked;
          const total = graph.total ?? 0;
          meta.textContent = total > 0
            ? t(assignmentPage?.olderCursor || assignmentPage?.newerCursor ? 'projectCoordination.history.sidebar' : 'projectCoordination.sidebarMeta', { active, total })
            : t('projectCoordination.sidebarIdle');
        } else {
          meta.textContent = t(!binding ? 'projectCoordination.error' : binding.availability === 'unbound'
            ? 'projectCoordination.unbound' : 'projectCoordination.unavailable');
        }
        if (available && report?.unread && report.latest) {
          meta.textContent = report.latest.title;
          const badge = document.createElement('span'); badge.className = 'coordination-leader-unread';
          badge.textContent = '•';
          badge.setAttribute('aria-label', `${t('projectCoordination.report.unread')} · ${coordinatorReportLabel(report.latest.kind)}`);
          leader.append(badge); row.dataset.unread = 'true';
          row.title = `${t('projectCoordination.report.unread')} · ${report.latest.title}`;
        }
        body.append(meta); row.append(mark, body);
        if (available) {
          const go = document.createElement('span'); go.className = 'coordination-leader-go';
          go.setAttribute('aria-hidden', 'true'); go.textContent = '›'; row.append(go);
        } else {
          const actions = document.createElement('span'); actions.className = 'coordination-leader-actions';
          if (binding?.availability === 'unbound') {
            const create = createButton({ label: t('projectCoordination.create'), size: 'sm', onClick: async () => {
              if (!valid() || creating || ports.canCreate?.() === false) return;
              creating = true; create.disabled = true;
              const feedback = document.createElement('p'); feedback.setAttribute('role', 'alert');
              actions.querySelector('[role="alert"]')?.remove();
              try { await ports.create(scope, binding); if (valid()) await refresh(true); }
              catch (error) {
                if (valid()) { feedback.textContent = error instanceof Error ? error.message : String(error); actions.append(feedback); }
              } finally { creating = false; create.disabled = false; }
            } });
            create.disabled = creating || ports.canCreate?.() === false;
            actions.append(create);
            if (ports.canCreate?.() === false) actions.append(coordinationText(t('projectCoordination.selectFolder'), 'small'));
          }
          actions.append(createButton({ label: t('projectCoordination.setup'), size: 'sm', variant: 'ghost', onClick: details }));
          row.append(actions);
        }
        ports.sidebar.append(row);
      }
      if (ports.mode === 'sidebar') {
        // The sidebar owns navigation only, never the active conversation canvas.
      } else if (binding?.availability === 'available' && binding.conversationId === scope.currentConversationId) {
        const fold = document.createElement('details'); fold.className = 'coordination-summary'; fold.open = expanded;
        fold.addEventListener('toggle', () => { expanded = fold.open; });
        const heading = document.createElement('summary');
        const summaryText = document.createElement('span'); summaryText.className = 'coordination-summary-title';
        summaryText.textContent = t('projectCoordination.taskGraph');
        heading.append(summaryText);
        if (graph.total !== null && graph.total > 0) {
          const tally = document.createElement('span'); tally.className = 'coordination-summary-tally';
          tally.textContent = t('projectCoordination.graph.tally', { completed: graph.completed, total: graph.total });
          heading.append(tally);
        }
        const actions = document.createElement('span'); actions.className = 'coordination-summary-actions';
        if (decisions?.length) actions.append(createButton({ label: `${t('projectCoordination.decisions')} · ${decisions.length}`, size: 'sm', onClick: details }));
        actions.append(createButton({ label: t('projectCoordination.controls'), size: 'sm', variant: 'ghost', onClick: details }));
        heading.append(actions); fold.append(heading);
        if (!board) fold.append(coordinationText(t('projectCoordination.boardUnavailable'), 'small'));
        if (!decisions) fold.append(coordinationText(t('projectCoordination.decisionsUnavailable'), 'small'));
        if (scope.dispatch) fold.append(createAssignmentHistoryNavigation(assignmentPage, historyCursor, cursor => {
          if (!valid()) return;
          historyCursor = cursor; graphState.showHistory = Boolean(cursor); delete graphState.selectedId;
          generation++; invalidate();
        }));
        if (!assignmentPage) fold.append(createButton({ label: t('projectCoordination.retry'), size: 'sm',
          variant: 'ghost', onClick: invalidate }));
        if (board || assignments?.length) fold.append(createCoordinationBoard(groups, navigate, 0, graphOptions,
          { pendingDecisions: decisions?.length ?? null, heading: false,
            viewState: graphState }));
        if (scope.budget) {
          const budgetView = createCoordinatorBudget(scope.budget, { path: scope.path, binding, valid });
          budgetView.update(budget); fold.append(budgetView.summary);
        }
        if (report?.latest && scope.reports) ports.canvas.append(createCoordinatorReport(report, {
          expanded: reportExpanded, toggle: open => { if (valid()) reportExpanded = open; },
          valid,
          markRead: request => scope.reports!.markRead(request),
          changed: () => { if (valid()) { generation++; invalidate(); } },
        }));
        if (reportUnavailable) {
          const warning = coordinationText(t('projectCoordination.report.unavailable'), 'small');
          warning.setAttribute('role', 'status'); ports.canvas.append(warning);
          ports.canvas.append(createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: invalidate }));
        }
        ports.canvas.append(fold);
      } else {
        const related = [...(board?.active || []), ...(board?.recentOutcomes || [])].some(work => work.conversationId === scope.currentConversationId)
          || assignments?.some(item => !item.revoked && item.request.recipientId === scope.currentConversationId);
        if (related && binding?.availability === 'available') ports.canvas.append(createButton({
          label: t('projectCoordination.returnCoordinator'), size: 'sm', variant: 'ghost', onClick: () => navigate(binding.conversationId),
        }));

      }
      if (!binding && ports.mode !== 'canvas') ports.sidebar.append(createButton({ label: t('projectCoordination.retry'), size: 'sm', onClick: invalidate }));

      const nextViewport = ports.canvas.querySelector('.coordination-graph-viewport');
      if (graphScroll && nextViewport) {
        nextViewport.scrollLeft = graphScroll[0]; nextViewport.scrollTop = graphScroll[1];
      }
      if (focusHost) {
        const replacement = [...focusHost.querySelectorAll<HTMLElement>('button, summary, [data-coordination-focus]')]
          .find(node => focusKey ? node.dataset.coordinationFocus === focusKey : node.textContent === focusLabel);
        replacement?.focus({ preventScroll: true });
      }
      ports.changed?.();
    } finally {
      flight = false;
      if (alive && pending) void refresh(true);
    }
  }
  return { refresh, destroy() { alive = false; generation++; unsubscribe?.(); ports.sidebar.replaceChildren(); ports.canvas.replaceChildren(); } };
}
