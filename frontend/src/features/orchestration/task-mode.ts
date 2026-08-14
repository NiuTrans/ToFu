import { orchestrationRegistry } from './registry';
import { formatOrchestrationEventLines } from './event-format';
import {
  normalizeOrchestrationOutcome,
  orchestrationOutcomeMessage,
  projectOrchestrationFinalResult,
} from './outcome-result';
import { createOrchestrationRequestLimits } from './request-limits';
import { orchestrationRequestFailureMessage } from './request-failure';
import { orchestrationResultError } from './result';
import { createOrchestrationRunSession } from './run-session';
import { orchestrationRunIsTerminal } from './run-status';
import { createTaskModeContractController } from './task-mode-contract-controller';
import { createTaskModeContractSession } from './task-mode-contract-session';
import { createTaskModeRootController } from './task-mode-root-controller';
import { createTaskModeRunListController } from './task-mode-list-controller';
import { createTaskModeRunListView } from './task-mode-list';
import { createTaskModeMutationReconciler } from './task-mode-mutation-reconciler';
import { createTaskModeNodePresentation } from './task-mode-node-presentation';
import { createTaskModePanelLayoutController } from './task-mode-panel-layout';
import { createTaskModeRunStore } from './task-mode-run-store';
import { createTaskModeServices } from './task-mode-services';
import { createTaskModeShell } from './task-mode-shell';
import { createTaskModeWorkspace } from './task-mode-workspace';

type Port = Record<string, unknown>;
type Services = ReturnType<typeof createTaskModeServices>;
type TaskModeBridge = Window & {
  _tmServices?: Services;
  [name: string]: unknown;
};

const bridge = orchestrationRegistry as unknown as unknown as TaskModeBridge;
const services = (): Services => {
  if (!bridge._tmServices) {
    throw new Error('Task Mode services are unavailable');
  }
  return bridge._tmServices;
};
const record = (value: unknown): Port => value
  && typeof value === 'object' && !Array.isArray(value) ? value as Port : {};
const invoke = <T = unknown>(
  port: Port | null | undefined,
  name: string,
  ...args: unknown[]
): T => {
  const operation = port?.[name];
  return (typeof operation === 'function'
    ? (operation as (...values: unknown[]) => unknown).apply(port, args)
    : undefined) as T;
};

let taskModeShell: ReturnType<typeof createTaskModeShell> | null = null;
let panelLayout: ReturnType<typeof createTaskModePanelLayoutController> | null = null;
const runSession = createOrchestrationRunSession();
const runStore = createTaskModeRunStore();
let runListController: ReturnType<typeof createTaskModeRunListController> | null = null;
let mutationReconciler: ReturnType<typeof createTaskModeMutationReconciler> | null = null;
let nodePresentation: ReturnType<typeof createTaskModeNodePresentation> | null = null;
let workspace: ReturnType<typeof createTaskModeWorkspace> | null = null;
let contractController: ReturnType<typeof createTaskModeContractController> | null = null;
const contracts = createTaskModeContractSession();
const limitPolicy = createOrchestrationRequestLimits({
  source: () => contracts.snapshot().requestLimits,
});
const rootController = createTaskModeRootController({
  services: services as unknown as () => Port,
  contractSession: contracts as unknown as Port,
  session: runSession as unknown as Port,
  runStore: runStore as unknown as Port,
  translate: (key: string, params?: unknown) => _tmT(key, params),
  reconcileRun: (mutation: unknown, runId: unknown) =>
    _tmReconcileRunMutation(mutation, runId),
  refreshRuns: () => _tmRefreshRuns(),
  openRun: (runId: unknown) => _tmOpenRun(runId),
  renderRunList: () => _tmRenderRunList(),
  isTerminal: (value: unknown) => _tmIsTerminal(value),
  projectTransition: (transition: unknown) =>
    _tmProjectRunTransition(transition),
  renderGraph: () => _tmRenderGraph(),
  renderInspector: (event: unknown) => _tmRenderInspector(event),
  renderTimelineEvent: (event: unknown) => _tmRenderTimelineEvent(event),
  presentPanel: (name: unknown, owner: unknown) =>
    _tmEnsurePanelLayout().present(name, owner),
  releasePanel: (owner: unknown, fallback: unknown) =>
    _tmEnsurePanelLayout().release(owner, fallback),
  syncChip: (status: unknown, done: unknown) => _tmSyncChip(status, done),
});

function _tmApiClient(): unknown { return rootController.apiClient(); }
function _tmStudioClient(): Port | null {
  const client = rootController.studioClient();
  return Object.keys(record(client)).length ? record(client) : null;
}
function _tmToast(message: unknown, isError?: unknown): unknown {
  return rootController.toast(message, isError);
}
function _tmTaskClient(): Port {
  return rootController.taskClient() as unknown as Port;
}
function _tmReportTaskFailure(context: string, value: unknown): boolean {
  return rootController.reportTaskFailure(context, value);
}
function _tmEnsureControllerHub() { return rootController.ensure(); }
function _tmEnsureActions(): Port { return rootController.actions(); }
function _tmEnsureCommands(): Port { return rootController.commands(); }
function _tmEnsureRunController(): Port { return rootController.run(); }
function _tmEnsureEventController(): Port { return rootController.events(); }

function _tmProjectRunTransition(transition: unknown): unknown {
  return workspacePort().projectTransition(record(transition));
}

function _tmResetEventState(): unknown {
  return invoke(_tmEnsureEventController(), 'reset');
}

function _tmIco(name: unknown): unknown { return services().icon(String(name)); }
function _tmT(key: string, params?: unknown): unknown {
  return services().translate(key, params);
}
function _tmEsc(value: unknown): unknown { return services().escape(value); }

function _tmEnsureNodePresentation() {
  if (nodePresentation) return nodePresentation;
  const capabilities = services();
  nodePresentation = createTaskModeNodePresentation({
    roles: capabilities.roles,
    controls: capabilities.controls,
    nodeRuntimeDefaults: () => contracts.snapshot().nodeRuntimeDefaults,
    glyphs: capabilities.glyphs,
    definition: () => workspacePort().definition(),
    iconSrc: (name: unknown) => capabilities.iconSrc(String(name)),
    icon: _tmIco,
    translate: _tmT,
    escape: _tmEsc,
  });
  return nodePresentation;
}

function _tmRoleDef(role: unknown): unknown {
  return _tmEnsureNodePresentation().roleDef(role);
}
function _tmControlDef(kind: unknown): unknown {
  return _tmEnsureNodePresentation().controlDef(kind);
}
function _tmNodeAccent(node: unknown): unknown {
  return _tmEnsureNodePresentation().accent(record(node));
}
function _tmNodeIconHtml(node: unknown): unknown {
  return _tmEnsureNodePresentation().iconHtml(record(node));
}
function _tmBindImageFallbacks(root: ParentNode | null): unknown {
  return _tmEnsureNodePresentation().bindImageFallbacks(root);
}
function _tmNodeGlyph(node: unknown): unknown {
  return _tmEnsureNodePresentation().glyph(record(node));
}
function _tmNodeLabel(node: unknown): unknown {
  return _tmEnsureNodePresentation().label(record(node));
}
function _tmNodeSub(node: unknown): unknown {
  return _tmEnsureNodePresentation().subtitle(record(node));
}

function workspacePort() {
  if (workspace) return workspace;
  const capabilities = services();
  workspace = createTaskModeWorkspace({
    document: capabilities.document,
    translate: _tmT,
    escape: _tmEsc,
    icon: _tmIco,
    report: (context: string, error: unknown) =>
      capabilities.reportError('TaskMode', context, error),
    contractSnapshot: () => contracts.snapshot(),
    runStore,
    runController: _tmEnsureRunController,
    eventController: _tmEnsureEventController,
    runListView: _tmEnsureRunListView,
    renderRunList: _tmRenderRunList,
    selectPanel: _tmSelectPanel,
    statusChip: _tmStatusChip,
    isTerminal: _tmIsTerminal,
    resultError: orchestrationResultError,
    failureMessage: (result: unknown, fallback: unknown) =>
      orchestrationRequestFailureMessage(result, _tmT, fallback),
    projectFinal: (run: unknown) => projectOrchestrationFinalResult(
      run, _tmT, contracts.snapshot().outcomeContract),
    onEdit: _tmOpenStudio,
    onDelete: _tmDeleteRun,
    onAbort: _tmAbortRun,
    onRetry: _tmOpenRun,
    onRerun: _tmRerun,
    nodeAccent: _tmNodeAccent,
    nodeIconHtml: _tmNodeIconHtml,
    nodeLabel: _tmNodeLabel,
    nodeSubtitle: _tmNodeSub,
    bindImageFallbacks: _tmBindImageFallbacks,
    formatEvent: formatOrchestrationEventLines,
    onApprove: _tmHumanApprove,
    onInput: _tmHumanInput,
    limitPolicy,
  });
  return workspace;
}

function _tmEnsureWorkspace() { return workspacePort(); }

export async function openTaskMode(runId?: unknown): Promise<unknown> {
  const shell = _tmEnsureShell();
  shell.open();
  const openOwner = shell.captureOpen();
  _tmEnsurePanelLayout().sync();
  await _tmRefreshAuthoringContract();
  if (!shell.ownsOpen(openOwner)) return false;
  const refresh = _tmRefreshRuns();
  const opened = runId ? await _tmOpenRun(runId) : true;
  await refresh;
  return opened;
}

export function closeTaskMode(event?: Event | null): boolean {
  return _tmEnsureShell().close(event);
}

function _tmAfterClose(): void {
  _tmEnsureContractController().invalidate();
  runListController?.invalidate();
  invoke(_tmEnsureRunController(), 'reset', { reason: 'close' });
}

async function _tmRefreshAuthoringContract(): Promise<boolean> {
  return _tmEnsureContractController().refresh();
}

function _tmEnsureContractController() {
  if (contractController) return contractController;
  contractController = createTaskModeContractController({
    session: contracts,
    source: _tmStudioClient,
    report: (error: unknown) => services().reportError(
      'TaskMode', 'authoring contract refresh', error),
    onAdopt: () => { workspacePort().refreshContract(); },
  });
  return contractController;
}

function _tmEnsureShell() {
  if (taskModeShell) return taskModeShell;
  const capabilities = services();
  taskModeShell = createTaskModeShell({
    document: capabilities.document as Document,
    window: capabilities.window as Window,
    translate: _tmT,
    escape: _tmEsc,
    icon: _tmIco,
    onOpenStudio: () => _tmOpenStudio(),
    onRefresh: _tmRefreshRuns,
    onPanelSelect: _tmSelectPanel,
    onClosed: _tmAfterClose,
  });
  return taskModeShell;
}

function _tmEnsureModal(): HTMLElement { return _tmEnsureShell().ensure(); }

function _tmEnsurePanelLayout() {
  if (panelLayout) return panelLayout;
  const capabilities = services();
  panelLayout = createTaskModePanelLayoutController({
    document: capabilities.document as Document,
    window: capabilities.window as Window,
  });
  return panelLayout;
}

function _tmSelectPanel(name: unknown): unknown {
  return _tmEnsurePanelLayout().select(name);
}

async function _tmRefreshRuns(): Promise<boolean> {
  return _tmEnsureRunListController().refresh();
}

function _tmEnsureRunListController() {
  if (runListController) return runListController;
  runListController = createTaskModeRunListController({
    store: runStore,
    client: _tmTaskClient,
    activeRunId: () => invoke(_tmEnsureRunController(), 'id'),
    report: _tmReportTaskFailure,
    projectActionState: (action) => taskModeShell?.setActionState(action),
    createView: () => createTaskModeRunListView({
      document: services().document as Document,
      hostId: 'tmRunList',
      translate: _tmT,
      escape: _tmEsc,
      richCopy: services().richCopy,
      icon: _tmIco,
      isTerminal: _tmIsTerminal,
      runContract: () => contracts.snapshot().runContract,
      normalizeOutcome: (run: unknown) => normalizeOrchestrationOutcome(
        run, contracts.snapshot().outcomeContract),
      outcomeMessage: (run: unknown, fallback: unknown) =>
        orchestrationOutcomeMessage(
          run, _tmT, fallback, contracts.snapshot().outcomeContract),
      failureMessage: (result: unknown, fallback: unknown) =>
        orchestrationRequestFailureMessage(result, _tmT, fallback),
      onOpen: _tmOpenRun,
      onLoadMore: () => _tmEnsureRunListController().loadMore(),
    }),
  });
  return runListController;
}

function _tmEnsureRunListView(): Port {
  return _tmEnsureRunListController().view();
}
function _tmSetRunListBusy(loading: unknown, placeholder?: unknown): unknown {
  return _tmEnsureRunListController().setBusy(loading, placeholder);
}
function _tmRenderRunList(): unknown { return _tmEnsureRunListController().render(); }

function _tmOpenStudio(orchId?: unknown): unknown {
  const studio = _tmStudioClient();
  if (!studio) {
    _tmToast(_tmT('tm.studioUnavailable'), true);
    return null;
  }
  closeTaskMode();
  return orchId
    ? invoke(studio, 'openDefinition', orchId)
    : invoke(studio, 'open');
}

function _tmStatusChip(value: unknown): unknown {
  return _tmEnsureRunListController().statusChip(value);
}
function _tmStatusLabel(value: unknown): unknown {
  return _tmEnsureRunListController().statusLabel(value);
}
function _tmAgo(value: unknown): unknown {
  return _tmEnsureRunListController().relativeTime(value);
}
function _tmDuration(value: unknown): unknown {
  return _tmEnsureRunListController().duration(value);
}

async function _tmOpenRun(runId: unknown): Promise<unknown> {
  return invoke(_tmEnsureRunController(), 'open', runId);
}
function _tmEnsureRunView(): Port { return workspacePort().runView(); }
function _tmRenderTitle(run: unknown, emptyKey?: string): unknown {
  return workspacePort().renderTitle(run, emptyKey);
}
function _tmIsTerminal(value: unknown): boolean {
  return orchestrationRunIsTerminal(
    value, () => contracts.snapshot().runContract);
}
function _tmSetTimelineBusy(value: unknown): unknown {
  return workspacePort().setTimelineBusy(value);
}
function _tmSyncChip(status: unknown, terminal?: unknown): boolean {
  return workspacePort().syncLifecycle(status, terminal);
}
async function _tmShowFinal(runId: unknown, owner: unknown): Promise<unknown> {
  return invoke(_tmEnsureRunController(), 'readFinal', runId, owner);
}
function _tmAdoptRunSnapshot(run: unknown, options?: Port): boolean {
  return workspacePort().adoptSnapshot(run, options);
}
function _tmClearRunSurface(transition?: Port): boolean {
  return workspacePort().clear(transition);
}
function _tmEnsureGraphView(): Port { return workspacePort().graphView(); }
function _tmRenderGraph(): unknown { return workspacePort().renderGraph(); }
function _tmEnsureTimelineView(): Port { return workspacePort().timelineView(); }
function _tmLine(html: string, className?: string): unknown {
  return workspacePort().line(html, className);
}
function _tmRenderTimelineEvent(event: unknown): unknown {
  return workspacePort().renderTimelineEvent(event);
}
function _tmRenderEvent(event: unknown): unknown {
  return invoke(_tmEnsureEventController(), 'ingest', event);
}
function _tmSelectNode(nodeId: unknown): unknown {
  return workspacePort().selectNode(nodeId);
}
function _tmEnsureInspectorView(): Port { return workspacePort().inspectorView(); }
function _tmRenderInspector(event?: unknown): unknown {
  return workspacePort().renderInspector(event);
}
function _tmTraceDetail(trace: unknown, event?: unknown): unknown {
  return invoke(_tmEnsureInspectorView(), 'traceDetail', trace, event);
}
function _tmGateCard(event: unknown, index: unknown): unknown {
  return invoke(_tmEnsureInspectorView(), 'gateCard', event, index);
}
async function _tmResyncRun(runId: unknown): Promise<unknown> {
  return invoke(_tmEnsureRunController(), 'resync', runId);
}
function _tmReconcileRunMutation(mutation: unknown, runId: unknown): boolean {
  return _tmEnsureMutationReconciler().reconcile(mutation, runId);
}
function _tmEnsureMutationReconciler() {
  if (mutationReconciler) return mutationReconciler;
  mutationReconciler = createTaskModeMutationReconciler({
    store: runStore,
    activeRunId: () => invoke(_tmEnsureRunController(), 'id'),
    renderList: _tmRenderRunList,
    renderTitle: _tmRenderTitle,
    refreshRuns: _tmRefreshRuns,
    resyncRun: _tmResyncRun,
    resetRun: (runId: unknown) => invoke(
      _tmEnsureRunController(), 'reset', { reason: 'missing', runId }),
  });
  return mutationReconciler;
}

async function _tmHumanApprove(requestId: unknown, approved: unknown) {
  return invoke(_tmEnsureCommands(), 'approveGate', requestId, approved);
}
async function _tmHumanInput(requestId: unknown, input: unknown) {
  return invoke(_tmEnsureCommands(), 'inputGate', requestId, input);
}
async function _tmAbortRun(runId: unknown) {
  return invoke(_tmEnsureCommands(), 'abortRun', runId);
}
async function _tmRerun(run: unknown) {
  return invoke(_tmEnsureCommands(), 'rerun', run);
}
async function _tmDeleteRun(runId: unknown) {
  return invoke(_tmEnsureCommands(), 'deleteRun', runId);
}

// Preserve the classic root's observable state ports during the cut-over.
// Accessors keep lazy controller identities current without duplicating state.
Object.defineProperties(bridge, {
  _tmShell: {
    configurable: true,
    get: () => taskModeShell,
    set: (value: ReturnType<typeof createTaskModeShell> | null) => {
      taskModeShell = value;
    },
  },
  _tmPanelLayout: {
    configurable: true,
    get: () => panelLayout,
    set: (value: ReturnType<typeof createTaskModePanelLayoutController> | null) => {
      panelLayout = value;
    },
  },
  _tmControllerHub: {
    configurable: true,
    get: () => rootController.current(),
    set: (value: unknown) => {
      rootController.replace(
        value as Parameters<typeof rootController.replace>[0]);
    },
  },
  _tmRunListController: {
    configurable: true,
    get: () => runListController,
    set: (value: ReturnType<typeof createTaskModeRunListController> | null) => {
      runListController = value;
    },
  },
  _tmMutationReconciler: {
    configurable: true,
    get: () => mutationReconciler,
    set: (value: ReturnType<typeof createTaskModeMutationReconciler> | null) => {
      mutationReconciler = value;
    },
  },
  _tmNodePresentation: {
    configurable: true,
    get: () => nodePresentation,
    set: (value: ReturnType<typeof createTaskModeNodePresentation> | null) => {
      nodePresentation = value;
    },
  },
  _tmWorkspace: {
    configurable: true,
    get: () => workspace,
    set: (value: ReturnType<typeof createTaskModeWorkspace> | null) => {
      workspace = value;
    },
  },
  _tmContractController: {
    configurable: true,
    get: () => contractController,
    set: (value: ReturnType<typeof createTaskModeContractController> | null) => {
      contractController = value;
    },
  },
});

Object.assign(bridge, {
  _tmRunSession: runSession,
  _tmRunStore: runStore,
  _tmContracts: contracts,
  _tmLimitPolicy: limitPolicy,
  openTaskMode,
  closeTaskMode,
  _tmApiClient,
  _tmStudioClient,
  _tmToast,
  _tmTaskClient,
  _tmReportTaskFailure,
  _tmEnsureControllerHub,
  _tmEnsureActions,
  _tmEnsureCommands,
  _tmEnsureRunController,
  _tmEnsureEventController,
  _tmProjectRunTransition,
  _tmResetEventState,
  _tmIco,
  _tmT,
  _tmEnsureNodePresentation,
  _tmRoleDef,
  _tmControlDef,
  _tmNodeAccent,
  _tmNodeIconHtml,
  _tmBindImageFallbacks,
  _tmNodeGlyph,
  _tmNodeLabel,
  _tmNodeSub,
  _tmEsc,
  _tmEnsureWorkspace,
  _tmAfterClose,
  _tmRefreshAuthoringContract,
  _tmEnsureContractController,
  _tmEnsureShell,
  _tmEnsureModal,
  _tmEnsurePanelLayout,
  _tmSelectPanel,
  _tmRefreshRuns,
  _tmEnsureRunListController,
  _tmEnsureRunListView,
  _tmSetRunListBusy,
  _tmRenderRunList,
  _tmOpenStudio,
  _tmStatusChip,
  _tmStatusLabel,
  _tmAgo,
  _tmDuration,
  _tmOpenRun,
  _tmEnsureRunView,
  _tmRenderTitle,
  _tmIsTerminal,
  _tmSetTimelineBusy,
  _tmSyncChip,
  _tmShowFinal,
  _tmAdoptRunSnapshot,
  _tmClearRunSurface,
  _tmEnsureGraphView,
  _tmRenderGraph,
  _tmEnsureTimelineView,
  _tmLine,
  _tmRenderTimelineEvent,
  _tmRenderEvent,
  _tmSelectNode,
  _tmEnsureInspectorView,
  _tmRenderInspector,
  _tmTraceDetail,
  _tmGateCard,
  _tmResyncRun,
  _tmReconcileRunMutation,
  _tmEnsureMutationReconciler,
  _tmHumanApprove,
  _tmHumanInput,
  _tmAbortRun,
  _tmRerun,
  _tmDeleteRun,
});
