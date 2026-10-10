/** project endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
import { createProjectPathWrite } from './project-path-singleflight';

import type { CoordinationBinding, ProjectMessagePage } from '../project-coordination.generated';
export function createProjectEndpoints(ports: EndpointPorts) {
  const { request, get, post, patch, _idempotentResponseWrite } = ports;
  const setPaths = createProjectPathWrite(request);
  const project = {
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
    board:         (path: string) =>
      get('/api/v4/project/board', { query: { path }, onError: 'null' }),
    coordination: (path: string) =>
      get<CoordinationBinding>('/api/v4/project/coordination', { query: { path }, onError: 'throw' }),
    coordinationMessages: (path: string, conversationId: string, beforeId = '') =>
      get<ProjectMessagePage>('/api/v4/project/coordination/messages',
        { query: { path, conversation_id: conversationId, before_id: beforeId }, onError: 'throw' }),
    bindCoordinator: (path: string, conversationId: string, expectedRevision: number) =>
      post<CoordinationBinding>('/api/v4/project/coordination/bind', { path, conversationId, expectedRevision },
           { headers: { 'Idempotency-Key': ports.newIdempotencyKey() } }),
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
