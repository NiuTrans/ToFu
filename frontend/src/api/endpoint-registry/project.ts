/** project endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
import { createProjectPathWrite } from './project-path-singleflight';

import { buildProjectContinuityEndpoints } from './project-continuity';

import type { CoordinatorConversationPage, CoordinatorReportState, CoordinatorReportReadRequest, CoordinationBinding, CoordinatorProgressState, ProjectMessagePage } from '../project-coordination.generated';
import type { RouterInstruction, RouterInstructionPage, RouterState, RouterWatchdogReviewRequest,
  RouterWatchdogState } from '../router.generated';

import type { ProjectDispatchRequest, ProjectDispatchRetry, ProjectDispatchRevoke,
  ProjectDispatchReviewRequest, ProjectDispatchPublishRequest, ProjectDispatchView,
  ProjectDispatchPage, ProjectDispatchListQuery,
  CoordinatorUsageState } from '../project-dispatch.generated';
// Bound read-only navigation demand; writes keep their own acknowledgement policy.
const COORDINATION_READ_TIMEOUT_MS = 12_000;

export function createProjectEndpoints(ports: EndpointPorts) {
  const { request, get, post, patch, _idempotentResponseWrite } = ports;
  const setPaths = createProjectPathWrite(request);
  const project = {

    ...buildProjectContinuityEndpoints(ports),
    status:        (convId: string)   => get('/api/v4/project/status'
                                     + (convId ? ('?conv_id=' + encodeURIComponent(convId)) : ''),
                                     { onError: 'null', coalesce: true }),
    setPaths:      (folders: readonly unknown[], readOnlyPaths?: readonly unknown[], recentPaths?: readonly unknown[]) =>
      setPaths(folders, readOnlyPaths, recentPaths),
    setPath:       (path: string)     =>
      request<Response | null>('/api/v4/project/set',
              { method: 'POST', json: { path }, parse: 'response' }),
    clear:         ()         =>
      request<Response | null>('/api/v4/project',
              { method: 'DELETE', parse: 'response', onError: 'null' }),
    recentList:    ()         => get('/api/v4/project/recent', {
      onError: 'null', coalesce: true
    }),
    recentAdd: (path: string) => post('/api/v4/project/recent', { path }),
    recentClear:   ()         =>
      _idempotentResponseWrite('/api/v4/project/recent',
                               { method: 'DELETE' }),
    // Advisory probe: nearest enclosing .git root for a staged folder.
    gitRootHint:   (path: string)   =>
      post('/api/v4/project/git-root-hint', { path }, { onError: 'null' }),
    // Re-key a renamed/moved project (recent + charter + board) to its new path.
    relinkRecent:  (oldPath: string, newPath: string) =>
      post('/api/v4/project/recent/relink', { oldPath, newPath }, { onError: 'null' }),
    rescan:        ()         =>
      request<Response | null>('/api/v4/project/rescan',
              { method: 'POST', parse: 'response' }),
    undo:          (body: JsonObject)     =>
      request<Response | null>('/api/v4/project/undo',
              { method: 'POST', json: body, parse: 'response' }),
    undoAll:       (body: JsonObject)     =>
      request<Response | null>('/api/v4/project/undo-all',
              { method: 'POST', json: body || {}, parse: 'response' }),
    // Re-apply a previously-undone round. Backend requires taskId; the caller
    // MUST also pin the conversation's own projectPath (undo deleted the
    // round's record, so redo resolves the project from the pin, never the
    // globally-active UI project). Mirrors undo's concurrency contract.
    redo:          (body: JsonObject)     =>
      request<Response | null>('/api/v4/project/redo',
              { method: 'POST', json: body, parse: 'response' }),
    browse:        (path: string, showHidden: boolean, opts?: EndpointOptions) =>
      post('/api/v4/project/browse', { path, showHidden: !!showHidden },
           Object.assign({
             timeout: 12000,
             rpcMethod: 'project.browse',
             rpcParams: { path, showHidden: !!showHidden },
           }, opts || {})),
    mkdir:         (parent: string, name: string)     =>
      request<Response | null>('/api/v4/project/mkdir',
              { method: 'POST', json: { parent, name }, parse: 'response' }),
    rmdir:         (path: string)             =>
      request<Response | null>('/api/v4/project/rmdir',
              { method: 'POST', json: { path }, parse: 'response' }),
    write:         (path: string, content: string)    => post('/api/v4/project/write', { path, content }),
    // Binary-safe drop-into-folder. `formData` carries file + dir (+ optional name).
    upload:        (formData: FormData)         =>
      request('/api/v4/project/upload', { method: 'POST', body: formData, timeout: 0 }),
    // Bounded .zip/.tar(.gz) drop → atomic extract into <dir>/<stem>/ + TeX candidates.
    uploadArchive: (formData: FormData)         =>
      request('/api/v4/project/upload-archive', { method: 'POST', body: formData, timeout: 0 }),
    writeApproval: (approvalId: string, approved: boolean) =>
      post('/api/v4/project/write-approval', { approvalId, approved }),
    // Project-brain Activity Feed (read-only). Keyed on the explicit `path`
    // the caller holds — never the server's global active project.
    feed:          (path: string, sinceSeq: string) =>
      get('/api/v4/project/feed',
          { query: { path, since: sinceSeq || 0 }, onError: 'null' }),
    // Charter and Board are read-only projections. Charter mutations only
    // happen through checker-backed human decision promotion below.
    charter:       (path: string) =>
      get('/api/v4/project/charter', { query: { path }, onError: 'null' }),
    board:         (path: string, signal?: AbortSignal) =>
      get('/api/v4/project/board', { query: { path }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'null' }),
    coordination: (path: string, signal?: AbortSignal) =>
      get<CoordinationBinding>('/api/v4/project/coordination', { query: { path }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    workerConversations: (path: string, cursor?: string, signal?: AbortSignal) =>
      get<CoordinatorConversationPage>('/api/v4/project/coordination/conversations',
        { query: { path, ...(cursor ? { cursor } : {}) }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    coordinatorReports: (path: string, signal?: AbortSignal) =>
      get<CoordinatorReportState>('/api/v4/project/coordination/reports', { query: { path }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    coordinatorBudget: (path: string, signal?: AbortSignal) =>
      get<CoordinatorUsageState>('/api/v4/project/coordination/budget', { query: { path }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    coordinatorProgress: (path: string, signal?: AbortSignal) =>
      get<CoordinatorProgressState>('/api/v4/project/coordination/progress', { query: { path }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    markCoordinatorReportRead: (body: CoordinatorReportReadRequest) =>
      post<CoordinatorReportState>('/api/v4/project/coordination/reports/read', body, { onError: 'throw' }),
    coordinationMessages: (path: string, conversationId: string, beforeId = '') =>
      get<ProjectMessagePage>('/api/v4/project/coordination/messages',
        { query: { path, conversation_id: conversationId, before_id: beforeId }, onError: 'throw' }),
    bindCoordinator: (path: string, conversationId: string, expectedRevision: number) =>
      post<CoordinationBinding>('/api/v4/project/coordination/bind', { path, conversationId, expectedRevision },
           { headers: { 'Idempotency-Key': ports.newIdempotencyKey() } }),
    createCoordinator: (path: string, folderId: string, expectedRevision: number) =>
      post<CoordinationBinding>('/api/v4/project/coordination/create', { path, folderId, expectedRevision },
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    // The Global Router is the bound Coordinator of the owner's router home (docs/ROUTER.md).
    globalRouter: (signal?: AbortSignal) =>
      get<RouterState>('/api/v4/router', { signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    ensureGlobalRouter: () =>
      post<RouterState>('/api/v4/router', {},
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    // Worker model of any bound Router (Global or Project); null returns to the owner default.
    routerWorkerModel: (path: string, signal?: AbortSignal) =>
      get<RouterState>('/api/v4/router/worker-model', { query: { path }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    setRouterWorkerModel: (path: string, workerModel: string | null) =>
      post<RouterState>('/api/v4/router/worker-model', { path, workerModel },
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    routerInstructions: (cursor?: string, signal?: AbortSignal) =>
      get<RouterInstructionPage>('/api/v4/router/instructions',
        { query: cursor ? { cursor } : {}, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    pauseRouterInstruction: (instructionId: string) =>
      post<RouterInstruction>('/api/v4/router/instructions/pause', { instructionId },
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    resumeRouterInstruction: (instructionId: string) =>
      post<RouterInstruction>('/api/v4/router/instructions/resume', { instructionId },
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    cancelRouterInstruction: (instructionId: string) =>
      post<RouterInstruction>('/api/v4/router/instructions/cancel', { instructionId },
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    // The Router Watchdog is the bound Coordinator of the owner's watchdog path (docs/ROUTER.md R7).
    routerWatchdog: (signal?: AbortSignal) =>
      get<RouterWatchdogState>('/api/v4/router/watchdog', { signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    ensureRouterWatchdog: () =>
      post<RouterWatchdogState>('/api/v4/router/watchdog', {},
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    configureRouterWatchdogReview: (body: RouterWatchdogReviewRequest) =>
      post<RouterWatchdogState>('/api/v4/router/watchdog/review', body,
        { headers: { 'Idempotency-Key': ports.newIdempotencyKey() }, onError: 'throw' }),
    newAssignmentCommandId: () => ports.newIdempotencyKey(),
    coordinationAssignments: (path: string, commandId?: string, options?: Pick<ProjectDispatchListQuery, 'cursor' | 'assignmentId'>, signal?: AbortSignal) =>
      get<ProjectDispatchPage>('/api/v4/project/coordination/assignments',
        { query: { path, ...(commandId ? { commandId } : {}), ...options }, signal, timeout: COORDINATION_READ_TIMEOUT_MS, onError: 'throw' }),
    grantAssignment: (body: ProjectDispatchRequest, commandId: string) =>
      post<ProjectDispatchView>('/api/v4/project/coordination/assignments', body,
        { headers: { 'Idempotency-Key': commandId }, onError: 'throw' }),
    dispatchAssignment: (body: ProjectDispatchRetry) =>
      post<ProjectDispatchView>('/api/v4/project/coordination/assignments/dispatch', body, { onError: 'throw' }),
    revokeAssignment: (body: ProjectDispatchRevoke) =>
      post<ProjectDispatchView>('/api/v4/project/coordination/assignments/revoke', body, { onError: 'throw' }),
    reviewAssignment: (body: ProjectDispatchReviewRequest) =>
      post<ProjectDispatchView>('/api/v4/project/coordination/assignments/review', body, { onError: 'throw' }),

    publishAssignment: (body: ProjectDispatchPublishRequest) =>
      post<ProjectDispatchView>('/api/v4/project/coordination/assignments/publish', body, { onError: 'throw' }),
    brainSummary:  (path: string) =>
      get('/api/v4/project/brain/summary',
          { query: { path }, onError: 'null' }),
    // Token-free Git integration control plane. These endpoints never ask an
    // agent to inspect or repair changes: immutable checkpoints are merged by
    // Git, and conflicts/gate failures are surfaced as quarantine records.
    integrationStatus: (path: string) =>
      get('/api/v4/project/integration/status',
          { query: { path }, onError: 'null' }),
    integrationCreate: (path: string, workId: string, title: string) =>
      post('/api/v4/project/integration/create', { path, workId, title: title || '' }),
    integrationRegister: (path: string, workId: string, workspacePath: string, title: string) =>
      post('/api/v4/project/integration/register',
           { path, workId, workspacePath, title: title || '' }),
    integrationCheckpoint: (path: string, workId: string) =>
      post('/api/v4/project/integration/checkpoint', { path, workId }),
    integrationSubmit: (path: string, workId: string) =>
      post('/api/v4/project/integration/submit', { path, workId }),
    integrationRetry: (path: string, workId: string) =>
      post('/api/v4/project/integration/retry', { path, workId }),
    integrationDiscard: (path: string, workId: string) =>
      post('/api/v4/project/integration/discard', { path, workId }),
    integrationPromote: (path: string, acknowledgeHeadDivergence: boolean) =>
      post('/api/v4/project/integration/promote', {
        path, acknowledgeHeadDivergence: !!acknowledgeHeadDivergence,
      }),
    integrationReconcileHead: (path: string) =>
      post('/api/v4/project/integration/reconcile-head', { path }),
    integrationPrune: (path: string) =>
      post('/api/v4/project/integration/prune', { path }),
    brainStatus:   (path: string) =>
      get('/api/v4/project/brain/status', { query: { path }, onError: 'null' }),
    brainWatchList: (path: string) =>
      get('/api/v4/project/brain/watch', { query: { path }, onError: 'null' }),
    brainWatchAdd: (path: string, kind: string, text: string, conversationId: string) =>
      post('/api/v4/project/brain/watch/add', {
        path, kind, text, conversationId: conversationId || '',
      }),
    brainWatchUpdate: (path: string, itemId: string, patch: JsonObject) =>
      post('/api/v4/project/brain/watch/update',
           Object.assign({ path, itemId }, patch || {})),
    brainWatchDelete: (path: string, itemId: string) =>
      post('/api/v4/project/brain/watch/delete', { path, itemId }),
    brainCheckers: (path: string) =>
      get('/api/v4/project/brain/checkers', { query: { path }, onError: 'null' }),
    brainCheckerRegister: (path: string, definition: JsonObject) =>
      post('/api/v4/project/brain/checkers/register', { path, definition }),
    brainCheckerRun: (path: string, checkerId: string, version: number, workId: string) =>
      post('/api/v4/project/brain/checkers/run', {
        path, checkerId, version, workId: workId || '',
      }),
    promoteDecision: (path: string, decision: JsonObject) =>
      post('/api/v4/project/charter/decision/promote',
           Object.assign({ path }, decision || {})),
  };
  return project;
}
