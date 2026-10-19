/** Project Coordinator workspace. Binding and work facts belong to the server.
 * Demand-only management tabs reuse existing application owners; conversation
 * navigation reuses the ordinary canvas. Closing disposes all view demand.
 */
import type { CoordinationCanvasState } from './project-coordination-canvas';
import { createButton, createDialog, createAlert, createEmptyState, createTabs } from '../ui';
import '../styles/application/33-project-coordination.css';
import { coordinationText as text, coordinationDecisions, createCoordinationBoard, createCoordinationDecisions, projectCoordinationWorkGroups } from './project-coordination-board';
export { projectCoordinationWorkGroups } from './project-coordination-board';
import { t } from '../i18n';

import { projectCoordinationGraph } from './project-coordination-graph';

import type { ProjectDispatchView, ProjectDispatchPage } from '../api/project-dispatch.generated';
import { createAssignmentHistoryNavigation } from './project-assignment-history';
import type { CoordinatorReportState, CoordinatorReportReadRequest, CoordinationBinding, ProjectMessagePage } from '../api/project-coordination.generated';
import { createProjectMessageReceipts } from './project-message-receipts';

import { createProjectDispatch, type ProjectDispatchPorts } from './project-dispatch';

import { createProjectScopeChanges, type ProjectScopeChangePorts } from './project-scope-changes';
import { createProjectResponsibilities, type ProjectResponsibilityPorts } from './project-responsibilities';
import { createProjectSharedFiles, type ProjectSharedFilesPorts } from './project-shared-files';
import { createCoordinatorBudget, type CoordinatorBudgetPorts } from './coordinator-budget';
import type { CoordinatorBudgetState } from '../api/project-dispatch.generated';
export interface CoordinationWork {
  id: string;
  conversationId?: string;
  title?: string;
  status: 'active' | 'completed' | 'failed' | 'cancelled';
  resultSummary?: string;
  changedPaths?: string[];
}
export interface ProjectCoordinationPorts {
  path: string;
  currentConversationId: string;
  currentPath(): string;

  currentConversation?(): string;

  dispatch?: ProjectDispatchPorts;

  scopeChanges?: ProjectScopeChangePorts;
  responsibilities?: ProjectResponsibilityPorts;
  sharedFiles?: ProjectSharedFilesPorts;
  conversations?(): Array<{ id: string; title: string }>;
  subscribe?(refresh: () => void): () => void;
  binding(path: string): Promise<CoordinationBinding>;

  reports?: {
    read(path: string): Promise<CoordinatorReportState>;
    markRead(request: CoordinatorReportReadRequest): Promise<CoordinatorReportState>;
  };
  budget?: CoordinatorBudgetPorts;
  messages?(path: string, conversationId: string, beforeId: string): Promise<ProjectMessagePage>;
  board(path: string): Promise<{ active: CoordinationWork[]; recentOutcomes: CoordinationWork[] } | null>;
  bind(path: string, conversationId: string, expectedRevision: number): Promise<CoordinationBinding>;

  create?(path: string, expectedRevision: number): Promise<CoordinationBinding>;
  navigate(conversationId: string): void;
  openEvidence(): void;
}

let activeDrawer: ReturnType<typeof createDialog> | null = null;

export async function openProjectCoordination(ports: ProjectCoordinationPorts): Promise<void> {

  activeDrawer?.close();
  const root = document.createElement('div');
  root.className = 'ui-stack';
  const drawer = createDialog({ title: t('projectCoordination.title'), body: root, appearance: 'project',
    actions: [{ id: 'close', label: t('projectCoordination.close') }] });
  drawer.el.classList.add('coordination-workspace');

  activeDrawer = drawer;
  document.body.append(drawer.el);
  let alive = true;
  let generation = 0;
  let activeTab = 'overview';
  let renderedBinding = '';
  let updateOverview: ((board: Awaited<ReturnType<ProjectCoordinationPorts['board']>>, assignments: ProjectDispatchView[] | null) => void) | undefined;
  let budgetView: ReturnType<typeof createCoordinatorBudget> | undefined;
  let unsubscribe: (() => void) | undefined;
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let refreshPending = false;
  let loading = false;
  let bindingPending = false;
  let refreshFeedback: HTMLElement | undefined;
  const graphState: CoordinationCanvasState = {};
  let historyCursor: string | undefined;
  let assignmentPage: ProjectDispatchPage | null = null;
  const closed = drawer.open().then(() => {
    alive = false; generation++; unsubscribe?.(); clearTimeout(refreshTimer); drawer.el.remove();
    if (activeDrawer === drawer) activeDrawer = null;
  });
  root.append(text(t('projectCoordination.loading')));
  const valid = () => alive && ports.currentPath().replace(/[/\\]+$/, '') === ports.path.replace(/[/\\]+$/, '')
    && (!ports.currentConversation || ports.currentConversation() === ports.currentConversationId);
  const navigate = (id: string) => {
    if (!valid()) { drawer.close(); return; }
    drawer.close();
    ports.navigate(id);
  };
  const error = (reason: unknown) => {
    if (!alive) return;
    root.replaceChildren(createAlert({ variant: 'error', title: t('projectCoordination.error'),
      body: reason instanceof Error ? reason.message : String(reason) }).el);
    root.append(createButton({ label: t('projectCoordination.retry'), onClick: () => { void load(); } }));
  };
  function scheduleRefresh() {
    if (!alive || loading || bindingPending || !refreshPending || activeTab !== 'overview' || refreshTimer !== undefined) return;
    // A fixed coalescing window cannot be postponed by a continuous event stream.
    refreshTimer = setTimeout(() => { refreshTimer = undefined; void load(true); }, 300);
  }
  const render = (binding: CoordinationBinding, board: { active: CoordinationWork[]; recentOutcomes: CoordinationWork[] } | null, assignments: ProjectDispatchView[] | null, budget: CoordinatorBudgetState | null = null) => {
    if (!valid()) { drawer.close(); return; }
    renderedBinding = JSON.stringify(binding);
    root.replaceChildren();
    const heading = document.createElement('div'); heading.className = 'ui-row ui-row--between ui-row--wrap';
    heading.append(text(ports.path, 'small'), createButton({ label: t('projectCoordination.refresh'), size: 'sm',
      onClick: () => { void load(); } }));
    root.append(heading);
    const entry = document.createElement('section');
    entry.className = 'ui-card ui-stack coordination-entry';
    entry.append(text(t('projectCoordination.coordinator'), 'h3'));
    const actions = document.createElement('div'); actions.className = 'ui-row ui-row--wrap';
    if (binding.availability === 'available') {
      entry.append(text(t('projectCoordination.sameCanvas')));
      actions.append(createButton({ label: t('projectCoordination.openCoordinator'), variant: 'primary', size: 'sm',
        onClick: () => navigate(binding.conversationId) }));
    } else {
      entry.append(text(t(binding.availability === 'unavailable'
        ? 'projectCoordination.unavailable' : 'projectCoordination.unbound')));
    }
    if (ports.create) {
      const button = createButton({ label: t(binding.availability === 'available' ? 'projectCoordination.replace' : 'projectCoordination.create'),
        size: 'sm', variant: 'ghost', onClick: async () => {
        if (!valid()) { drawer.close(); return; }
        if (bindingPending) return;
        button.disabled = true;
        bindingPending = true;
        const own = ++generation;
        try {
          const updated = await ports.create!(ports.path, binding.revision);
          if (own === generation && valid()) { render(updated, board, assignments); refreshPending = true; }
        } catch (reason) {
          if (own === generation && valid()) error(reason);
        } finally { bindingPending = false; button.disabled = false; scheduleRefresh(); }
      } });
      actions.append(button);
    }
    if (actions.childElementCount) entry.append(actions);
    if (ports.create) entry.append(text(t('projectCoordination.cleanConversation'), 'small'));
    root.append(entry);
    const overview = document.createElement('div'); overview.className = 'ui-stack';
    const feedback = document.createElement('div'); feedback.className = 'ui-stack';
    const overviewContent = document.createElement('div'); overviewContent.className = 'ui-stack';
    overviewContent.tabIndex = -1;
    overview.append(feedback, overviewContent); refreshFeedback = feedback;
    const team = document.createElement('div'); team.className = 'ui-stack';
    const controls = document.createElement('div'); controls.className = 'ui-stack';
    const messages = document.createElement('div'); messages.className = 'ui-stack';
    const resources = document.createElement('div'); resources.className = 'ui-stack';
    let controlsLoaded = false, resourcesLoaded = false, messagesLoaded = false;
    let reveal = () => {};
    const tabs = createTabs({ activeId: activeTab, tabs: [
      { id: 'overview', label: t('projectCoordination.overview'), panel: overview },
      { id: 'team', label: t('projectCoordination.team'), panel: team },
      { id: 'messages', label: t('projectCoordination.communication'), panel: messages },
      { id: 'resources', label: t('projectCoordination.resources'), panel: resources },
      { id: 'controls', label: t('projectCoordination.controls'), panel: controls },
    ], onChange: id => { activeTab = id; reveal(); scheduleRefresh(); } });
    root.append(tabs.el);
    const scope = { path: ports.path, conversationId: ports.currentConversationId, binding,
      valid, readBinding: ports.binding };
    budgetView = ports.budget ? createCoordinatorBudget(ports.budget, scope) : undefined;
    if (budgetView) {
      budgetView.update(budget); overview.append(budgetView.summary); controls.append(budgetView.settings);
    }
    reveal = () => {
      if (activeTab === 'controls' && !controlsLoaded) {
        controlsLoaded = true;
        if (ports.dispatch) controls.append(createProjectDispatch(ports.dispatch, { ...scope, navigate }));
        if (ports.dispatch && ports.scopeChanges) controls.append(createProjectScopeChanges(ports.scopeChanges, ports.dispatch, scope));
        if (ports.dispatch && ports.responsibilities) controls.append(createProjectResponsibilities(ports.responsibilities, ports.dispatch, scope));
      }
      if (activeTab === 'resources' && !resourcesLoaded) {
        resourcesLoaded = true;
        if (ports.sharedFiles) resources.append(createProjectSharedFiles(ports.sharedFiles, scope, ports.dispatch));
        resources.append(createButton({ label: t('projectCoordination.evidence'), onClick: () => {
          if (!valid()) { drawer.close(); return; }
          drawer.close(); ports.openEvidence();
        } }));
      }
      if (activeTab === 'messages' && !messagesLoaded) {
        messagesLoaded = true;
        messages.append(text(t('projectCoordination.communicationHint')));
        if (ports.messages && ports.currentConversationId) messages.append(createProjectMessageReceipts(
          beforeId => ports.messages!(ports.path, ports.currentConversationId, beforeId), valid));
      }
    };
    reveal();

    const roster = new Map((ports.conversations?.() || ports.dispatch?.recipients() || []).map(item => [item.id, item]));
    for (const work of [...(board?.active || []), ...(board?.recentOutcomes || [])]) {
      if (work.conversationId && !roster.has(work.conversationId)) roster.set(work.conversationId, { id: work.conversationId, title: work.title || work.conversationId });
    }
    team.append(text(t('projectCoordination.teamHint')));
    const teamGrid = document.createElement('div'); teamGrid.className = 'ui-grid coordination-work-grid';
    for (const member of [...roster.values()].filter(item => item.id !== binding.conversationId).slice(0, 100)) {
      const card = document.createElement('article'); card.className = 'ui-card ui-stack ui-stack--tight';
      card.append(text(member.title || member.id, 'strong'));
      if (member.id === ports.currentConversationId) card.append(text(t('projectCoordination.currentConversation'), 'small'));
      card.append(createButton({ label: t('projectCoordination.openWorker'), size: 'sm', onClick: () => navigate(member.id) }));
      teamGrid.append(card);
    }
    team.append(teamGrid);
    if (!teamGrid.childElementCount) team.append(createEmptyState({ title: t('projectCoordination.empty.team') }));
    let overviewSignature = '';
    updateOverview = (nextBoard, nextAssignments) => {
      const groups = projectCoordinationWorkGroups(nextBoard?.active || [], nextBoard?.recentOutcomes || []);
      const graphOptions = { assignments: nextAssignments, coordinatorId: binding.conversationId, activeConversationId: ports.currentConversationId };
      const decisions = coordinationDecisions(nextAssignments || []);
      // Compare the bounded graph projection, never whole execution result bodies.
      const signature = JSON.stringify([nextBoard !== null, projectCoordinationGraph(groups, graphOptions),
        historyCursor, assignmentPage?.olderCursor, assignmentPage?.newerCursor,
        nextAssignments === null ? null : decisions]);
      if (signature === overviewSignature) return;
      overviewSignature = signature;
      const focused = document.activeElement instanceof HTMLElement && overviewContent.contains(document.activeElement)
        ? document.activeElement : null;
      const focusKey = focused?.dataset.coordinationFocus;
      overviewContent.replaceChildren();
      if (ports.dispatch) overviewContent.append(createAssignmentHistoryNavigation(assignmentPage, historyCursor, cursor => {
        if (!valid() || loading) return;
        historyCursor = cursor; graphState.showHistory = Boolean(cursor); delete graphState.selectedId;
        void load(true);
      }));
      if (!nextBoard) overviewContent.append(createAlert({ variant: 'warning', title: t('projectCoordination.boardUnavailable'),
        body: t('projectCoordination.boardUnavailableBody') }).el);
      if (nextBoard || nextAssignments?.length) overviewContent.append(createCoordinationBoard(
        groups, navigate, nextAssignments === null ? null : decisions.length, graphOptions,
        { pendingDecisions: nextAssignments === null ? null : decisions.length,
          viewState: graphState }));

      if (nextAssignments) overviewContent.append(createCoordinationDecisions(decisions, () => tabs.activate('controls')));
      else if (ports.dispatch) overviewContent.append(createAlert({ variant: 'warning', title: t('projectCoordination.decisionsUnavailable') }).el);
      if (focused) {
        const replacement = focusKey && [...overviewContent.querySelectorAll<HTMLElement>('[data-coordination-focus]')]
          .find(node => node.dataset.coordinationFocus === focusKey);
        (replacement || overviewContent).focus({ preventScroll: true });
      }
    };
    updateOverview(board, assignments);
    root.append(text(t('projectCoordination.deliveryCaveat'), 'small'));
  };
  async function load(background = false) {
    if (background && activeTab !== 'overview') return;
    if (!valid()) { drawer.close(); return; }
    if (loading || bindingPending) { refreshPending = true; return; }
    loading = true; refreshPending = false;
    clearTimeout(refreshTimer); refreshTimer = undefined;
    const own = ++generation;
    if (!background) root.replaceChildren(text(t('projectCoordination.loading')));
    try {
      // Wait for every read even when binding fails, so an early rejection cannot
      // release the slot while the board or assignment request is still running.
      const [bindingRead, boardRead, assignmentsRead, budgetRead] = await Promise.allSettled([
        ports.binding(ports.path), ports.board(ports.path),
        ports.dispatch ? ports.dispatch.list(ports.path, undefined, historyCursor ? { cursor: historyCursor } : undefined)
          : Promise.resolve({ assignments: [], olderCursor: null, newerCursor: null }),
        ports.budget ? ports.budget.read(ports.path) : Promise.resolve(null)]);
      if (own !== generation) return;
      if (!valid()) { drawer.close(); return; }
      if (background && activeTab !== 'overview') { refreshPending = true; return; }
      if (bindingRead.status === 'rejected') throw bindingRead.reason;
      const binding = bindingRead.value;
      const board = boardRead.status === 'fulfilled' ? boardRead.value : null;
      assignmentPage = assignmentsRead.status === 'fulfilled' ? assignmentsRead.value : null;
      const assignments = assignmentPage?.assignments ?? null;
      const budget = budgetRead.status === 'fulfilled' ? budgetRead.value : null;
      refreshFeedback?.replaceChildren();
      if (background && renderedBinding === JSON.stringify(binding) && updateOverview) {
        updateOverview(board, assignments); budgetView?.update(budget);
      } else render(binding, board, assignments, budget);
    } catch (reason) {
      if (own !== generation) return;
      if (!valid()) { drawer.close(); return; }
      if (background && activeTab !== 'overview') { refreshPending = true; return; }
      if (background && refreshFeedback?.isConnected) refreshFeedback.replaceChildren(
        createAlert({ variant: 'warning', title: t('projectCoordination.error'),
          body: reason instanceof Error ? reason.message : String(reason) }).el,
        createButton({ label: t('projectCoordination.retry'), onClick: () => { void load(true); } }));
      else error(reason);
    } finally { loading = false; scheduleRefresh(); }
  }
  unsubscribe = ports.subscribe?.(() => {
    // Preserve in-progress forms and message pagination. The overview alone
    // refreshes from committed project events, with no idle polling.
    refreshPending = true;
    scheduleRefresh();
  });
  await load();
  await closed;
}
