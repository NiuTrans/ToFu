/** Demand-only coordinator workspace. Normal conversations remain the canvas;
 * this modal owns read demand and view state, never execution or transcript state.
 */
import type { CoordinationCanvasState } from './project-coordination-canvas';
import { createButton, createDialog, createAlert, createTabs } from '../ui';
import '../styles/application/33-project-coordination.css';
import { coordinationText as text, coordinationDecisions, coordinationTaskDigest, createCoordinationBoard, createCoordinationDecisions, projectCoordinationWorkGroups } from './project-coordination-board';
export { projectCoordinationWorkGroups } from './project-coordination-board';
import { t } from '../i18n';
import { projectCoordinationGraph } from './project-coordination-graph';
import { createCoordinationWorkList, type CoordinationConversation } from './project-coordination-work-list';
import { captureCoordinationView, createCoordinationReference } from './project-coordination-reference';
import { createCoordinationManagement } from './project-coordination-management';
import type { ProjectDispatchView, ProjectDispatchPage, CoordinatorBudgetState } from '../api/project-dispatch.generated';
import { createAssignmentHistoryNavigation } from './project-assignment-history';
import type { CoordinatorReportState, CoordinatorReportReadRequest, CoordinationBinding, ProjectMessagePage, CoordinatorConversationPage } from '../api/project-coordination.generated';
import { createProjectMessageReceipts } from './project-message-receipts';
import { createProjectDispatch, type ProjectDispatchPorts } from './project-dispatch';
import { createProjectScopeChanges, type ProjectScopeChangePorts } from './project-scope-changes';
import { createProjectResponsibilities, type ProjectResponsibilityPorts } from './project-responsibilities';
import { createProjectSharedFiles, type ProjectSharedFilesPorts } from './project-shared-files';
import { createCoordinatorBudget, type CoordinatorBudgetPorts } from './coordinator-budget';

export interface CoordinationWork {
  id: string; conversationId?: string; title?: string;
  status: 'active' | 'completed' | 'failed' | 'cancelled';
  resultSummary?: string; changedPaths?: string[];
}
export interface ProjectCoordinationPorts {
  path: string; currentConversationId: string; currentPath(): string;
  currentConversation?(): string;
  dispatch?: ProjectDispatchPorts;
  scopeChanges?: ProjectScopeChangePorts;
  responsibilities?: ProjectResponsibilityPorts;
  sharedFiles?: ProjectSharedFilesPorts;
  conversations?(): CoordinationConversation[];
  workerConversations?(path: string, cursor?: string): Promise<CoordinatorConversationPage>;
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
  inspect?(conversationId: string): void;
  /** Prepares a visible ordinary draft; does not send or mutate context. */
  diagnose?(conversationId: string): void;
  openEvidence(): void;
}

let activeDrawer: ReturnType<typeof createDialog> | null = null;
export async function openProjectCoordination(ports: ProjectCoordinationPorts): Promise<void> {
  activeDrawer?.close();
  const root = document.createElement('div'); root.className = 'ui-stack';
  const drawer = createDialog({ title: t('projectCoordination.title'), body: root, appearance: 'project',
    actions: [{ id: 'close', label: t('projectCoordination.close') }] });
  drawer.el.classList.add('coordination-workspace'); activeDrawer = drawer; document.body.append(drawer.el);
  let alive = true, generation = 0, activeTab = 'overview', loading = false, refreshPending = false, bindingPending = false;
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let unsubscribe: (() => void) | undefined;
  let binding: CoordinationBinding | undefined;
  let board: Awaited<ReturnType<ProjectCoordinationPorts['board']>> = null;
  let assignmentPage: ProjectDispatchPage | null = null;
  let workerPage: CoordinatorConversationPage | null = null;
  let rosterUnavailable = false;
  let historyCursor: string | undefined, rosterCursor: string | undefined;
  const graphState: CoordinationCanvasState = {}, teamState: CoordinationCanvasState = {};
  let updateView: ((budget: CoordinatorBudgetState | null) => void) | undefined;
  let management: ReturnType<typeof createCoordinationManagement> | undefined;
  const feedback = document.createElement('div'); feedback.className = 'coordination-refresh-feedback'; feedback.setAttribute('role', 'status');
  const closed = drawer.open().then(() => {
    alive = false; generation++; unsubscribe?.(); clearTimeout(refreshTimer); management?.destroy(); drawer.el.remove();
    if (activeDrawer === drawer) activeDrawer = null;
  });
  root.append(text(t('projectCoordination.loading')));
  const valid = () => alive && ports.currentPath().replace(/[/\\]+$/, '') === ports.path.replace(/[/\\]+$/, '')
    && (!ports.currentConversation || ports.currentConversation() === ports.currentConversationId);
  const navigate = (id: string) => { if (valid()) { drawer.close(); ports.navigate(id); } else drawer.close(); };
  const inspect = ports.inspect ? (id: string) => { if (valid()) { drawer.close(); ports.inspect!(id); } } : undefined;
  const diagnose = ports.diagnose ? (id: string) => { if (valid()) { drawer.close(); ports.diagnose!(id); } } : undefined;
  const fail = (reason: unknown) => {
    if (!valid()) return;
    if (!feedback.isConnected) root.replaceChildren(feedback);
    feedback.replaceChildren(createAlert({ variant: 'warning', title: t('projectCoordination.error'),
      body: reason instanceof Error ? reason.message : String(reason) }).el,
    createButton({ label: t('projectCoordination.retry'), onClick: () => { void load(); } }));
  };
  const conversations = (): CoordinationConversation[] => workerPage ? workerPage.conversations.map(item => ({
    id: item.conversationId, title: item.title, turnCount: item.turnCount,
  })) : ports.conversations?.() || ports.dispatch?.recipients?.() || [];
  function scheduleRefresh() {
    if (!alive || loading || bindingPending || !refreshPending || activeTab !== 'overview' || refreshTimer !== undefined) return;
    refreshTimer = setTimeout(() => { refreshTimer = undefined; void load(true); }, 300);
  }
  function mount() {
    root.replaceChildren();
    const heading = document.createElement('div'); heading.className = 'ui-row ui-row--between ui-row--wrap';
    heading.append(text(ports.path, 'small'), createButton({ label: t('projectCoordination.refresh'), size: 'sm', onClick: () => { void load(); } }));
    const entry = document.createElement('section'); entry.className = 'ui-card ui-stack coordination-entry';
    const overview = document.createElement('div'); overview.className = 'ui-stack';
    const overviewContent = document.createElement('div'); overviewContent.className = 'ui-stack'; overviewContent.tabIndex = -1;
    overview.append(overviewContent);
    const team = document.createElement('div'); team.className = 'ui-stack';
    const controls = document.createElement('div'); controls.className = 'ui-stack';
    const messages = document.createElement('div'); messages.className = 'ui-stack';
    const resources = document.createElement('div'); resources.className = 'ui-stack';
    const controlForms = document.createElement('div'); controlForms.className = 'ui-stack';
    const controlFeedback = document.createElement('div'); controlFeedback.setAttribute('role', 'status');
    let controlsLoaded = false, resourcesLoaded = false, messagesLoaded = false;
    let controlsRevision: number | undefined;
    let reveal = () => {};
    const tabs = createTabs({ activeId: activeTab, tabs: [
      { id: 'overview', label: t('projectCoordination.overview'), panel: overview },
      { id: 'team', label: t('projectCoordination.team'), panel: team },
      { id: 'messages', label: t('projectCoordination.communication'), panel: messages },
      { id: 'resources', label: t('projectCoordination.resources'), panel: resources },
      { id: 'controls', label: t('projectCoordination.controls'), panel: controls },
    ], onChange: id => { activeTab = id; reveal(); scheduleRefresh(); } });
    root.append(heading, feedback, entry, tabs.el, text(t('projectCoordination.deliveryCaveat'), 'small'));
    const scope = () => {
      const snapshot = binding!;
      return { path: ports.path, conversationId: ports.currentConversationId, binding: snapshot,
        valid: () => valid() && binding?.revision === snapshot.revision, readBinding: ports.binding };
    };
    const budgetView = ports.budget ? createCoordinatorBudget(ports.budget, scope()) : undefined;
    if (budgetView) { overview.append(budgetView.summary); controls.append(budgetView.settings); }
    management = createCoordinationManagement(ports, { binding: () => binding!, valid, refresh: () => load(), inspect, diagnose });
    controls.append(management.el, controlFeedback, controlForms);
    reveal = () => {
      if (activeTab === 'controls' && !controlsLoaded) {
        controlsLoaded = true; controlsRevision = binding!.revision;
        const current = scope();
        if (ports.dispatch) controlForms.append(createProjectDispatch(ports.dispatch, { ...current, navigate }));
        if (ports.dispatch && ports.scopeChanges) controlForms.append(createProjectScopeChanges(ports.scopeChanges, ports.dispatch, current));
        if (ports.dispatch && ports.responsibilities) controlForms.append(createProjectResponsibilities(ports.responsibilities, ports.dispatch, current));
      }
      if (activeTab === 'resources' && !resourcesLoaded) {
        resourcesLoaded = true;
        if (ports.sharedFiles) resources.append(createProjectSharedFiles(ports.sharedFiles, scope(), ports.dispatch));
        resources.append(createButton({ label: t('projectCoordination.evidence'), onClick: () => {
          if (!valid()) { drawer.close(); return; } drawer.close(); ports.openEvidence();
        } }));
      }
      if (activeTab === 'messages' && !messagesLoaded) {
        messagesLoaded = true; messages.append(text(t('projectCoordination.communicationHint')));
        if (ports.messages && ports.currentConversationId) messages.append(createProjectMessageReceipts(
          beforeId => ports.messages!(ports.path, ports.currentConversationId, beforeId), valid));
      }
    };
    let entrySignature = '', overviewSignature = '', teamSignature = '';
    updateView = budget => {
      const restore = captureCoordinationView(root);
      if (entrySignature !== JSON.stringify(binding)) {
        entrySignature = JSON.stringify(binding);
        entry.replaceChildren(text(t('projectCoordination.coordinator'), 'h3'));
        if (binding!.availability === 'available') entry.append(createButton({ label: t('projectCoordination.openCoordinator'),
          variant: 'primary', size: 'sm', onClick: () => navigate(binding!.conversationId) }),
        createCoordinationReference({ conversationId: binding!.conversationId }, 'coordinator', inspect));
        else entry.append(text(t(binding!.availability === 'unavailable' ? 'projectCoordination.unavailable' : 'projectCoordination.unbound')));
        if (ports.create && binding!.availability === 'unbound') {
          const create = createButton({ label: t('projectCoordination.create'), size: 'sm', onClick: async () => {
            if (!valid() || bindingPending) return;
            bindingPending = true; create.disabled = true;
            try { await ports.create!(ports.path, binding!.revision); }
            catch (reason) { entrySignature = ''; fail(reason); return; }
            finally { bindingPending = false; }
            await load();
          } }); entry.append(create);
        }
      }
      if (controlsRevision !== undefined && controlsRevision !== binding!.revision) {
        controlForms.inert = true;
        controlFeedback.replaceChildren(text(t('projectCoordination.replacement.draftsKept')));
      }
      const assignments = assignmentPage?.assignments ?? null;
      const groups = projectCoordinationWorkGroups(board?.active || [], board?.recentOutcomes || []);
      const options = { assignments, coordinatorId: binding!.conversationId, activeConversationId: ports.currentConversationId };
      const graph = projectCoordinationGraph(groups, options);
      const roster = conversations();
      const decisions = assignments === null ? null : coordinationDecisions(assignments);
      const nextOverview = JSON.stringify([board !== null, graph, decisions, historyCursor, assignmentPage?.olderCursor,
        assignmentPage?.newerCursor, roster, rosterUnavailable, workerPage?.nextCursor]);
      if (nextOverview !== overviewSignature) {
        overviewSignature = nextOverview; overviewContent.replaceChildren();
        if (ports.dispatch) overviewContent.append(createAssignmentHistoryNavigation(assignmentPage, historyCursor, cursor => {
          if (!valid() || loading) return;
          historyCursor = cursor; graphState.showHistory = Boolean(cursor); graphState.workPage = 0; delete graphState.selectedId; void load();
        }));
        if (!board) overviewContent.append(createAlert({ variant: 'warning', title: t('projectCoordination.boardUnavailable'),
          body: t('projectCoordination.boardUnavailableBody') }).el);
        overviewContent.append(createCoordinationBoard(groups, navigate, decisions?.length ?? null, options,
          { viewState: graphState, conversations: roster, inspect, manage: () => tabs.activate('controls') }));
        if (decisions) overviewContent.append(createCoordinationDecisions(decisions, () => tabs.activate('controls')));
        else if (ports.dispatch) overviewContent.append(createAlert({ variant: 'warning', title: t('projectCoordination.decisionsUnavailable') }).el);
        if (!board || assignments === null || rosterUnavailable) overviewContent.append(createButton({ label: t('projectCoordination.retry'),
          size: 'sm', onClick: () => { void load(); } }));
        if (rosterUnavailable) overviewContent.append(text(t('projectCoordination.roster.unavailable'), 'small'));
        if (ports.workerConversations && workerPage?.nextCursor) overviewContent.append(createButton({ label: t('projectCoordination.roster.more'),
          size: 'sm', variant: 'ghost', onClick: () => tabs.activate('team') }));
      }
      const nextTeam = JSON.stringify([graph, roster, rosterCursor, workerPage?.nextCursor, rosterUnavailable]);
      if (nextTeam !== teamSignature) {
        teamSignature = nextTeam;
        team.replaceChildren(text(t('projectCoordination.teamHint')),
          createCoordinationWorkList(graph, navigate, { state: teamState, conversations: roster, team: true, inspect, digest: coordinationTaskDigest }));
        team.append(text(t(ports.workerConversations ? 'projectCoordination.roster.coverage' : 'projectCoordination.roster.catalogCoverage'), 'small'));
        if (workerPage?.nextCursor) team.append(createButton({ label: t('projectCoordination.roster.more'), size: 'sm', onClick: () => {
          if (loading) return; rosterCursor = workerPage!.nextCursor!; teamState.workPage = 0; void load();
        } }));
        if (rosterCursor) team.append(createButton({ label: t('projectCoordination.roster.first'), size: 'sm', variant: 'ghost', onClick: () => {
          if (loading) return; rosterCursor = undefined; teamState.workPage = 0; void load();
        } }));
        if (rosterUnavailable) team.append(text(t('projectCoordination.roster.unavailable')),
          createButton({ label: t('projectCoordination.retry'), size: 'sm', onClick: () => { void load(); } }));
      }
      budgetView?.update(budget); reveal(); restore();
    };
  }
  async function load(background = false): Promise<boolean> {
    if (background && activeTab !== 'overview') return false;
    if (!valid()) { drawer.close(); return false; }
    if (loading || bindingPending) { refreshPending = true; return false; }
    loading = true; refreshPending = false; clearTimeout(refreshTimer); refreshTimer = undefined;
    const own = ++generation;
    drawer.el.setAttribute('aria-busy', 'true');
    try {
      const [bindingRead, boardRead, assignmentsRead, budgetRead, workerRead] = await Promise.allSettled([
        ports.binding(ports.path), ports.board(ports.path),
        ports.dispatch ? ports.dispatch.list(ports.path, undefined, historyCursor ? { cursor: historyCursor } : undefined)
          : Promise.resolve({ assignments: [], olderCursor: null, newerCursor: null }),
        ports.budget ? ports.budget.read(ports.path) : Promise.resolve(null),
        ports.workerConversations ? ports.workerConversations(ports.path, rosterCursor) : Promise.resolve(null),
      ]);
      if (own !== generation) return false;
      if (!valid()) { drawer.close(); return false; }
      if (background && activeTab !== 'overview') { refreshPending = true; return false; }
      if (bindingRead.status === 'rejected') throw bindingRead.reason;
      binding = bindingRead.value;
      board = boardRead.status === 'fulfilled' ? boardRead.value : null;
      assignmentPage = assignmentsRead.status === 'fulfilled' ? assignmentsRead.value : null;
      rosterUnavailable = workerRead.status === 'rejected';
      if (workerRead.status === 'fulfilled') workerPage = workerRead.value;
      feedback.replaceChildren();
      if (!updateView) mount();
      updateView!(budgetRead.status === 'fulfilled' ? budgetRead.value : null);
      return true;
    } catch (reason) { if (own === generation && valid()) fail(reason); return false; }
    finally { loading = false; drawer.el.removeAttribute('aria-busy'); scheduleRefresh(); }
  }
  unsubscribe = ports.subscribe?.(() => { refreshPending = true; scheduleRefresh(); });
  await load(); await closed;
}
