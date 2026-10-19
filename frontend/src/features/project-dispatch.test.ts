// @vitest-environment jsdom
/** Minimal consultation safety specs: no paid calls, explicit human admission only. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CoordinationBinding } from '../api/project-coordination.generated';
import type { ProjectDispatchRequest, ProjectDispatchView } from '../api/project-dispatch.generated';
import type { ProjectDispatchPorts } from './project-dispatch';
import { createProjectEndpoints } from '../api/endpoint-registry/project';
import type { EndpointPorts } from '../api/endpoint-registry/contracts';
vi.mock('../i18n', () => ({ t: (key: string, params?: Record<string, unknown>) => `${key}${params ? ':' + JSON.stringify(params) : ''}` }));
let mount: typeof import('./project-dispatch').createProjectDispatch;
beforeEach(async () => {
  vi.resetModules(); mount = (await import('./project-dispatch')).createProjectDispatch;
  document.body.replaceChildren();
});
const binding: CoordinationBinding = { projectPath: '/project', conversationId: 'coord', revision: 7,
  availability: 'available', capabilities: { assign: false, sendMessage: false, steerSubagent: false } };
function view(request: ProjectDispatchRequest): ProjectDispatchView {
  return { assignmentId: 'assignment', request, grantRevision: 3, revoked: false,
    execution: null, review: null, canonicalDelivery: false, dispatch: 'deferred' };
}
function setup() {
  let valid = true;
  const p: ProjectDispatchPorts = {
    recipients: () => [{ id: 'worker', title: '<worker>' }, { id: 'coord', title: 'Coordinator' }],
    humanTurns: () => [{ actor: 'human', turnId: 'human', projectionRevision: 4 }],
    list: vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [] })), grant: vi.fn(async request => view(request)),
    dispatch: vi.fn(), revoke: vi.fn(), review: vi.fn(), publish: vi.fn(), commandId: vi.fn(() => 'stable-command'),
  };
  const scope = { path: '/project', conversationId: 'coord', binding,
    valid: () => valid, readBinding: vi.fn(async () => binding), navigate: vi.fn() };
  const root = mount(p, scope); document.body.append(root);
  return { p, scope, root, stale: () => { valid = false; } };
}
function control(name: string) { return document.querySelector<HTMLInputElement>(`[name="${name}"]`)!; }
function button(key: string) { return [...document.querySelectorAll('button')].find(n => n.textContent === key)!; }
function fill() {
  control('turnId').value = 'human'; control('turnId').dispatchEvent(new Event('change'));
  control('recipientId').value = 'worker'; control('brief').value = 'Consult only <unsafe>';
  control('model').value = 'test.fake-model'; control('maxTokens').value = '100';
  control('maxModelRequests').value = '2'; control('timeout').value = '60';
}
function consent() {
  const node = document.querySelector<HTMLInputElement>('input[name="consent"]')!;
  node.checked = true; node.dispatchEvent(new Event('change'));
}
function submit() { document.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true })); }
const flushed = () => new Promise(resolve => setTimeout(resolve, 0));

describe('explicit no-tool consultation', () => {
  it('mount and refresh only GET; empty consent and model do not buy execution', async () => {
    const { p } = setup(); await flushed();
    expect(p.list).toHaveBeenCalledTimes(1); expect(p.grant).not.toHaveBeenCalled();
    expect(document.querySelector<HTMLInputElement>('input[name="consent"]')!.checked).toBe(false);
    expect(control('model').value).toBe('');
    expect(button('projectDispatch.submit').disabled).toBe(true);
    fill(); submit(); await flushed(); expect(p.grant).not.toHaveBeenCalled();
    button('projectDispatch.refresh').click(); await flushed(); expect(p.list).toHaveBeenCalledTimes(2);
    expect(p.dispatch).not.toHaveBeenCalled();
    expect(document.body.innerHTML).not.toContain('<worker>');
  });
  it('consented request carries exact human proof and no inferred tool authority', async () => {
    const { p } = setup(); fill(); consent(); submit();
    await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    const [body, key] = vi.mocked(p.grant).mock.calls[0];
    expect(body).toEqual({ projectPath: '/project', coordinatorId: 'coord', recipientId: 'worker',
      authorizationSource: { conversationId: 'coord', turnId: 'human', projectionRevision: 4 },
      brief: 'Consult only <unsafe>', model: 'test.fake-model', budget: {
        maxTokens: 100, maxModelRequests: 2, deadline: expect.any(Number) } });
    expect(key).toBe('stable-command'); expect(body.budget.deadline).toBeGreaterThan(Date.now() / 1000);
    await flushed(); expect(button('projectDispatch.submit').disabled).toBe(true);
  });
  it('double submit is blocked; uncertain retry survives teardown with identical body and key', async () => {
    const { p, scope, root } = setup();
    let reject!: (reason: Error) => void;
    p.grant = vi.fn(() => new Promise<ProjectDispatchView>((_resolve, no) => { reject = no; }));
    fill(); consent(); submit(); submit();
    await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(button('projectDispatch.retryExact').disabled).toBe(true);
    const [original, key] = vi.mocked(p.grant).mock.calls[0];
    reject(new Error('lost response')); await flushed();
    root.remove(); p.grant = vi.fn(async request => view(request));
    document.body.append(mount(p, scope));
    expect(control('brief').disabled).toBe(true);
    expect(document.querySelector('fieldset')).toBeNull();
    control('brief').value = 'must not change retained body'; control('timeout').value = '99999';
    submit(); submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(vi.mocked(p.grant).mock.calls[0]).toEqual([original, key]);
    expect(p.commandId).toHaveBeenCalledOnce();
    await flushed();
    fill(); consent(); submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledTimes(2));
    expect(p.commandId).toHaveBeenCalledTimes(2);
  });
  it('assistant or stale human source cannot authorize', async () => {
    const { p } = setup(); fill(); consent(); control('projectionRevision').value = '5'; submit(); await flushed();
    expect(p.grant).not.toHaveBeenCalled();
    control('turnId').value = 'assistant'; control('projectionRevision').value = '4'; submit(); await flushed();
    expect(p.grant).not.toHaveBeenCalled();
  });
  it('stale path/conversation and re-bound coordinator fence even an already clicked grant', async () => {
    const { p, scope, stale } = setup();
    let resolve!: (value: CoordinationBinding) => void;
    scope.readBinding = vi.fn(() => new Promise(r => { resolve = r; }));
    fill(); consent(); submit(); stale(); resolve(binding); await flushed();
    expect(p.grant).not.toHaveBeenCalled();
  });
  it('pre-send binding failure does not retain an unsent authorization or block a new scope', async () => {
    const { p, scope, root } = setup(); scope.readBinding = vi.fn(async () => { throw new Error('binding read failed'); });
    fill(); consent(); submit(); await flushed(); expect(p.grant).not.toHaveBeenCalled();
    root.remove(); scope.readBinding = vi.fn(async () => binding); document.body.append(mount(p, scope));
    fill(); consent(); submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(p.commandId).toHaveBeenCalledTimes(2);
  });
  it('refresh recovers an uncertain authorization only via a matching command-ID receipt, never an empty list', async () => {
    const { p } = setup(); p.grant = vi.fn(async () => { throw new Error('lost response'); });
    fill(); consent(); submit(); await flushed();
    const [request] = vi.mocked(p.grant).mock.calls[0];
    p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [] }));
    button('projectDispatch.refresh').click(); await flushed();
    expect(p.list).toHaveBeenCalledWith('/project', 'stable-command');
    expect(button('projectDispatch.retryExact')).toBeDefined(); expect(control('brief').disabled).toBe(true);
    p.list = vi.fn(async (_path, command) => ({ olderCursor: null, newerCursor: null, assignments: command ? [view(request)] : [] }));
    button('projectDispatch.refresh').click(); await flushed();
    expect(p.grant).toHaveBeenCalledOnce(); expect(button('projectDispatch.submit')).toBeDefined();
    expect(control('brief').disabled).toBe(false);
    expect(document.querySelector<HTMLInputElement>('input[name="consent"]')!.checked).toBe(false);
  });
  it('unknown or mismatched command-ID receipts cannot discard an uncertain authorization', async () => {
    const { p } = setup(); p.grant = vi.fn(async () => { throw new Error('uncertain'); });
    fill(); consent(); submit(); await flushed();
    const [request] = vi.mocked(p.grant).mock.calls[0];
    p.list = vi.fn(async (_path, command) => {
      if (command) return { olderCursor: null, newerCursor: null, assignments: [view({ ...request, model: 'different' })] };
      return { olderCursor: null, newerCursor: null, assignments: [view(request)] };
    });
    button('projectDispatch.refresh').click(); await flushed();
    expect(button('projectDispatch.retryExact')).toBeDefined();
    p.list = vi.fn(async (_path, command) => { if (command) throw new Error('unknown'); return { olderCursor: null, newerCursor: null, assignments: [] }; });
    button('projectDispatch.refresh').click(); await flushed();
    expect(button('projectDispatch.retryExact')).toBeDefined(); expect(p.grant).toHaveBeenCalledOnce();
  });
  it('allows explicit proof fields only when no real human turns are available', async () => {
    const { p, scope, root } = setup(); root.remove(); p.humanTurns = () => [];
    document.body.append(mount(p, scope)); fill(); control('projectionRevision').value = '9'; consent(); submit();
    await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(vi.mocked(p.grant).mock.calls[0][0].authorizationSource).toEqual({ conversationId: 'coord', turnId: 'human', projectionRevision: 9 });
  });
});

describe('assignment receipt actions', () => {
  const request: ProjectDispatchRequest = { projectPath: '/project', coordinatorId: 'coord', recipientId: 'worker',
    authorizationSource: { conversationId: 'coord', turnId: 'human', projectionRevision: 4 },
    model: 'test.fake-model', brief: '<unsafe>', budget: { maxTokens: 100, maxModelRequests: 1, deadline: 2000000000 } };
  const completed = (): ProjectDispatchView => ({ ...view(request), execution: { turnId: 'output', attemptId: 'attempt', taskId: 'task',
    status: 'completed', resultRevision: 11, result: { content: 'advice' } } });
  it('completion and accept remain separate from canonical delivery; review pins both revisions', async () => {
    const { p } = setup(); const receipt = completed(); p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [receipt] }));
    await flushed(); button('projectDispatch.refresh').click(); await flushed();
    expect(document.body.textContent).toContain('projectDispatch.notDelivered');
    expect(document.body.textContent).toContain('projectDispatch.notReviewed');
    expect(document.body.innerHTML).not.toContain('<unsafe>');
    p.review = vi.fn(async () => ({ ...receipt, review: { decision: 'accept' as const, note: '', resultRevision: 11 } }));
    button('projectDispatch.accept').click();
    await vi.waitFor(() => expect(p.review).toHaveBeenCalledWith({ projectPath: '/project', assignmentId: 'assignment',
      decision: 'accept', note: '', resultRevision: 11, expectedRevision: 3 }));
    expect(document.body.textContent).toContain('projectDispatch.notDelivered');
  });
  it('non-completed results have no review; revoke uses grantRevision and navigation existing worker', async () => {
    const { p, scope } = setup(); const receipt = completed(); receipt.execution!.status = 'running';
    p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [receipt] })); p.revoke = vi.fn(async () => ({ ...receipt, revoked: true }));
    await flushed(); button('projectDispatch.refresh').click(); await flushed();
    expect(button('projectDispatch.accept')).toBeUndefined();
    button('projectCoordination.openWorker').click(); expect(scope.navigate).toHaveBeenCalledWith('worker');
    button('projectDispatch.revoke').click();
    await vi.waitFor(() => expect(p.revoke).toHaveBeenCalledWith({ projectPath: '/project', assignmentId: 'assignment', expectedRevision: 3 }));
    expect(p.dispatch).not.toHaveBeenCalled();
  });
  it('late scope responses and stale action clicks are ignored', async () => {
    const { p, stale } = setup(); p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [completed()] }));
    await flushed(); button('projectDispatch.refresh').click(); await flushed(); stale();
    button('projectDispatch.accept').click(); button('projectDispatch.revoke').click(); button('projectCoordination.openWorker').click();
    await flushed(); expect(p.review).not.toHaveBeenCalled(); expect(p.revoke).not.toHaveBeenCalled();
  });
  it('unavailable or non-current coordinator is read-only, regardless of assign capability', async () => {
    const { p, scope, root } = setup(); root.remove(); scope.binding = { ...binding, conversationId: 'other' };
    p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [completed()] })); document.body.append(mount(p, scope)); await flushed();
    fill(); consent(); submit(); await flushed(); expect(p.grant).not.toHaveBeenCalled();
    expect(button('projectDispatch.accept')).toBeUndefined(); expect(button('projectDispatch.revoke')).toBeUndefined();
  });
  it('an immutable old-result review is labelled stale and cannot imply current acceptance or re-review', async () => {
    const { p } = setup(); const receipt = completed();
    receipt.review = { decision: 'accept', note: 'old', resultRevision: 10 };
    p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [receipt] }));
    await flushed(); button('projectDispatch.refresh').click(); await flushed();
    expect(document.body.textContent).toContain('projectDispatch.staleReview');
    const reviewLabel = [...document.querySelectorAll('p')].find(node => node.textContent?.startsWith('projectDispatch.review:'))!;
    expect(JSON.parse(reviewLabel.textContent!.slice('projectDispatch.review:'.length)).decision).toContain('"revision":10');
    expect(button('projectDispatch.accept')).toBeUndefined(); expect(button('projectDispatch.rework')).toBeUndefined();
    expect(document.body.textContent).toContain('projectDispatch.notDelivered');
  });
  it('typed facade uses v4 paths, exact JSON and the caller-owned Idempotency-Key', async () => {
    const get = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [] })); const post = vi.fn(async () => view(request));
    const api = createProjectEndpoints({ get, post, request: vi.fn(), newIdempotencyKey: () => 'id' } as unknown as EndpointPorts);
    await api.coordinationAssignments('/project'); await api.grantAssignment(request, 'same-key');

    await api.coordinationAssignments('/project', 'same-key');
    expect(get).toHaveBeenCalledWith('/api/v4/project/coordination/assignments', { query: { path: '/project', commandId: 'same-key' }, onError: 'throw' });
    await api.revokeAssignment({ projectPath: '/project', assignmentId: 'assignment', expectedRevision: 3 });
    expect(get).toHaveBeenCalledWith('/api/v4/project/coordination/assignments', { query: { path: '/project' }, onError: 'throw' });
    expect(post).toHaveBeenCalledWith('/api/v4/project/coordination/assignments', request, {
      headers: { 'Idempotency-Key': 'same-key' }, onError: 'throw' });
    expect(post).toHaveBeenCalledWith('/api/v4/project/coordination/assignments/revoke', {
      projectPath: '/project', assignmentId: 'assignment', expectedRevision: 3 }, { onError: 'throw' });
  });
});

const fileProfile = () => ({ mode: 'files' as const, tools: ['read_files' as const, 'edit_file' as const],
  readPaths: ['src', 'docs/readme.md'], writePaths: ['src'], maxToolCalls: 8 });
function fillFiles() {
  fill(); control('mode').value = 'files'; control('mode').dispatchEvent(new Event('change'));
  control('readPaths').value = 'src, docs/readme.md'; control('writePaths').value = 'src';
  control('maxToolCalls').value = '8'; control('edit_file').checked = true;
  control('edit_file').dispatchEvent(new Event('change'));
}
const fileResult = (): ProjectDispatchView => ({ assignmentId: 'old-assignment', grantRevision: 6, revoked: false,
  request: { projectPath: '/project', coordinatorId: 'coord', recipientId: 'worker', brief: 'Fix src/task.ts', model: 'old-model',
    authorizationSource: { conversationId: 'coord', turnId: 'human', projectionRevision: 4 },
    budget: { maxTokens: 300, maxModelRequests: 2, deadline: 2000000000 }, profile: fileProfile() },
  execution: { turnId: 'output', attemptId: 'attempt', taskId: 'task', status: 'completed', resultRevision: 11, result: {} },
  review: null, canonicalDelivery: false,
  delivery: { taskId: 'task', status: 'blocked', reason: 'project_publication_authority_missing', checkpointSha: 'checkpoint-sha', targetSha: '' } });

async function showReceipt(receipt: ProjectDispatchView) {
  const state = setup(); state.p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [receipt] }));
  await flushed(); button('projectDispatch.refresh').click(); await flushed(); return state;
}

describe('explicit bounded file work', () => {
  it('default consult hides file scope; only explicit file opt-in exposes the three structured tools', () => {
    setup(); expect(control('mode').value).toBe('consult');
    expect(control('readPaths').disabled).toBe(true);
    expect(control('read_files').disabled).toBe(true); expect(control('read_files').checked).toBe(true);
    expect(control('write_file').checked).toBe(false); expect(control('edit_file').checked).toBe(false);
    expect(control('maxToolCalls').value).toBe('');
    expect(document.querySelector('[name="run_command"]')).toBeNull();
    fillFiles(); expect(control('readPaths').disabled).toBe(false); expect(control('read_files').disabled).toBe(true);
  });
  it('sends the exact selected file profile only after fresh consent, then resets to consult', async () => {
    const { p } = setup(); fillFiles(); submit(); await flushed(); expect(p.grant).not.toHaveBeenCalled();
    consent(); submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(vi.mocked(p.grant).mock.calls[0][0].profile).toEqual(fileProfile());
    await flushed(); expect(control('mode').value).toBe('consult'); expect(control('consent').checked).toBe(false);
    expect(control('write_file').checked).toBe(false); expect(control('edit_file').checked).toBe(false);
    expect(control('read_files').checked).toBe(true);
  });
  it('scope, model or budget edits uncheck previously given consent', () => {
    setup(); fillFiles(); consent(); expect(control('consent').checked).toBe(true);
    for (const name of ['readPaths', 'maxToolCalls', 'model', 'maxTokens', 'mode', 'write_file']) {
      consent(); control(name).dispatchEvent(new Event('input'));
      expect(control('consent').checked).toBe(false); expect(button('projectDispatch.submit').disabled).toBe(true);
    }
  });
  it.each(['/etc', '../src', 'src/../other', 'src\\alias', '~', 'src//task.ts', '.git', 'src/.tofu-state',
    'data', 'src/project.json', 'logs', 'uploads', 'src%2fother'])('rejects non-relative or fenced read scope %s', async path => {
    const { p } = setup(); fillFiles(); control('readPaths').value = path; consent(); submit(); await flushed();
    expect(p.grant).not.toHaveBeenCalled(); expect(document.body.textContent).toContain('projectDispatch.invalidProfile');
  });
  it.each(['-1', '0', '513', '1.5', ''])('rejects missing/out-of-range file tool budget %s', async calls => {
    const { p } = setup(); fillFiles(); control('maxToolCalls').value = calls; consent(); submit(); await flushed();
    expect(p.grant).not.toHaveBeenCalled();
  });
  it('read-only file work needs no write scope; dot is an explicit whole-project opt-in', async () => {
    const { p } = setup(); fillFiles(); control('edit_file').checked = false;
    control('writePaths').value = ''; control('readPaths').value = '.'; consent(); submit();
    await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(vi.mocked(p.grant).mock.calls[0][0].profile).toEqual({ mode: 'files', tools: ['read_files'], readPaths: ['.'], writePaths: [], maxToolCalls: 8 });
  });
  it.each(['', 'src-sibling', 'docs'])('write/edit rejects absent or uncovered write prefix %s', async writes => {
    const { p } = setup(); fillFiles(); control('writePaths').value = writes; consent(); submit(); await flushed();
    expect(p.grant).not.toHaveBeenCalled();
  });
  it('lost file grant survives drawer reopening with its exact profile and key', async () => {
    const { p, root, scope } = setup(); p.grant = vi.fn(async () => { throw new Error('lost'); });
    fillFiles(); consent(); submit(); await flushed();
    const original = vi.mocked(p.grant).mock.calls[0]; root.remove(); document.body.append(mount(p, scope));
    expect(control('mode').value).toBe('files'); expect(control('readPaths').value).toBe('src, docs/readme.md');
    expect(control('edit_file').checked).toBe(true); expect(control('maxToolCalls').disabled).toBe(true);
    control('readPaths').value = '.'; control('edit_file').checked = false;
    p.grant = vi.fn(async request => view(request)); submit(); await flushed();
    expect(vi.mocked(p.grant).mock.calls[0]).toEqual(original); expect(p.commandId).toHaveBeenCalledOnce();
  });
  it.each(['mode', 'tools', 'readPaths', 'writePaths', 'maxToolCalls', 'missing', 'reworkOf'])('mismatched %s ACK/lookup never releases unknown grant', async changed => {
    const { p } = setup();
    const mismatch = (request: ProjectDispatchRequest): ProjectDispatchRequest => {
      const copy = structuredClone(request);
      if (changed === 'missing') delete copy.profile;
      else if (changed === 'reworkOf') copy.reworkOf = { assignmentId: 'other', grantRevision: 2, resultRevision: 3 };
      else if (changed === 'mode') copy.profile!.mode = 'consult';
      else if (changed === 'tools') copy.profile!.tools = ['read_files'];
      else if (changed === 'maxToolCalls') copy.profile!.maxToolCalls++;
      else copy.profile![changed as 'readPaths' | 'writePaths'] = ['other'];
      return copy;
    };
    p.grant = vi.fn(async request => view(mismatch(request)));
    fillFiles(); consent(); submit(); await flushed(); const [request] = vi.mocked(p.grant).mock.calls[0];
    expect(button('projectDispatch.retryExact')).toBeDefined(); expect(control('mode').disabled).toBe(true);
    p.list = vi.fn(async (_path, key) => ({ olderCursor: null, newerCursor: null, assignments: key ? [view(mismatch(request))] : [] }));
    button('projectDispatch.refresh').click(); await flushed(); expect(control('mode').disabled).toBe(true);
    p.list = vi.fn(async (_path, key) => ({ olderCursor: null, newerCursor: null, assignments: key ? [view(request)] : [] }));
    button('projectDispatch.refresh').click(); await flushed(); expect(control('mode').disabled).toBe(false);
  });
  it('uses a human prompt preview safely while retaining exact turn proof', async () => {
    const { p, root, scope } = setup(); root.remove();
    p.humanTurns = () => [{ actor: 'human', turnId: 'human', projectionRevision: 4, promptPreview: 'Please check <src> changes' }];
    document.body.append(mount(p, scope));
    expect(control('turnId').textContent).toContain('Please check <src> changes · human · 4');
    expect(document.querySelector('src')).toBeNull(); fill(); consent(); submit(); await flushed();
    expect(vi.mocked(p.grant).mock.calls[0][0].authorizationSource.turnId).toBe('human');
  });
});

describe('fresh rework and explicit publication', () => {
  it('review rework adds no authority; prepare is a local exact-version draft with all source/model/budgets/consent blank', async () => {
    const receipt = fileResult(); receipt.review = { decision: 'rework', note: 'Fix evidence', resultRevision: 11 };
    const { p } = await showReceipt(receipt); fillFiles(); consent();
    button('projectDispatch.prepareRework').click(); await flushed();
    for (const name of ['turnId', 'projectionRevision', 'model', 'maxTokens', 'maxModelRequests', 'timeout', 'maxToolCalls']) {
      expect(control(name).value).toBe('');
    }
    expect(control('consent').checked).toBe(false); expect(control('brief').value).toBe('Fix src/task.ts');
    expect(control('recipientId').value).toBe('worker'); expect(control('mode').value).toBe('files');
    expect(document.body.textContent).toContain('"assignmentId":"old-assignment","grantRevision":6,"resultRevision":11');
    expect(p.grant).not.toHaveBeenCalled(); expect(p.dispatch).not.toHaveBeenCalled();
    expect(p.review).not.toHaveBeenCalled(); expect(p.publish).not.toHaveBeenCalled();
    submit(); await flushed(); expect(p.grant).not.toHaveBeenCalled();
    fillFiles(); consent(); submit(); await flushed();
    expect(vi.mocked(p.grant).mock.calls[0][0].reworkOf).toEqual({ assignmentId: 'old-assignment', grantRevision: 6, resultRevision: 11 });
  });
  it('retains exact rework lineage across uncertain remount; changed predecessor ACK cannot free it', async () => {
    const receipt = fileResult(); receipt.review = { decision: 'rework', note: '', resultRevision: 11 };
    const { p, root, scope } = await showReceipt(receipt); button('projectDispatch.prepareRework').click();
    p.grant = vi.fn(async request => view({ ...request, reworkOf: { ...request.reworkOf!, resultRevision: 12 } }));
    fillFiles(); consent(); submit(); await flushed(); const original = vi.mocked(p.grant).mock.calls[0];
    root.remove(); document.body.append(mount(p, scope)); await flushed();
    expect(control('brief').disabled).toBe(true); expect(document.body.textContent).toContain('"resultRevision":11');
    p.grant = vi.fn(async request => view(request)); submit(); await flushed();
    expect(vi.mocked(p.grant).mock.calls[0]).toEqual(original);
  });
  it('accept is not publication; a separate click publishes the exact grant/result only', async () => {
    const receipt = fileResult(); const { p } = await showReceipt(receipt);
    p.review = vi.fn(async () => { receipt.review = { decision: 'accept', note: '', resultRevision: 11 }; return receipt; });
    expect(button('projectDispatch.publish')).toBeUndefined(); button('projectDispatch.accept').click(); await flushed();
    expect(p.publish).not.toHaveBeenCalled(); expect(document.body.textContent).toContain('projectDispatch.notDelivered');
    p.publish = vi.fn(async () => { receipt.publication = { subject: 'owner', requestedAt: 1, grantRevision: 6, resultRevision: 11, checkpointSha: 'checkpoint-sha' }; return receipt; });
    button('projectDispatch.publish').click(); await flushed();
    expect(p.publish).toHaveBeenCalledWith({ projectPath: '/project', assignmentId: 'old-assignment', expectedRevision: 6,
      resultRevision: 11, expectedCheckpointSha: 'checkpoint-sha' });
    expect(p.grant).not.toHaveBeenCalled(); expect(p.dispatch).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain('projectDispatch.publicationRequested');
    expect(document.body.textContent).toContain('projectDispatch.notDelivered'); expect(button('projectDispatch.publish')).toBeUndefined();
  });
  it.each(['oldReview', 'running', 'revoked', 'consult', 'rework', 'missingCheckpoint'])('no publish button for %s', async state => {
    const receipt = fileResult(); receipt.review = { decision: 'accept', note: '', resultRevision: 11 };
    if (state === 'oldReview') receipt.review.resultRevision = 10;
    if (state === 'running') receipt.execution!.status = 'running';
    if (state === 'revoked') receipt.revoked = true;
    if (state === 'consult') delete receipt.request.profile;
    if (state === 'missingCheckpoint') receipt.delivery!.checkpointSha = '';
    if (state === 'rework') receipt.review.decision = 'rework';
    const { p } = await showReceipt(receipt); expect(button('projectDispatch.publish')).toBeUndefined(); expect(p.publish).not.toHaveBeenCalled();
    if (state === 'oldReview') expect(button('projectDispatch.prepareRework')).toBeUndefined();
  });
  it('receipt diagnostics are text-safe; only canonicalDelivery=true projects delivered, never status or acceptance', async () => {
    const receipt = fileResult(); receipt.delivery!.reason = '<reason>'; receipt.delivery!.status = 'delivered';
    receipt.review = { decision: 'accept', note: '', resultRevision: 11 };
    await showReceipt(receipt);
    expect(document.body.textContent).toContain('projectDispatch.heldAuthorization');
    expect(document.body.textContent).toContain('checkpoint-sha'); expect(document.body.textContent).toContain('<reason>');
    expect(document.querySelector('reason')).toBeNull(); expect(document.body.textContent).toContain('projectDispatch.notDelivered');
    receipt.canonicalDelivery = true; button('projectDispatch.refresh').click(); await flushed();
    expect(document.body.textContent).toContain('projectDispatch.delivered'); expect(document.body.textContent).not.toContain('projectDispatch.notDelivered');
  });
  it('canonical flag without an existing receipt cannot display delivery', async () => {
    const receipt = fileResult(); receipt.canonicalDelivery = true; delete receipt.delivery;
    await showReceipt(receipt);
    expect(document.body.textContent).toContain('projectDispatch.notDelivered');
    expect(document.body.textContent).not.toContain('projectDispatch.delivered');
  });
  it('stale publish click is fenced by fresh binding read and sends no write', async () => {
    const receipt = fileResult(); receipt.review = { decision: 'accept', note: '', resultRevision: 11 };
    const { p, scope } = await showReceipt(receipt);
    scope.readBinding = vi.fn(async () => ({ ...binding, revision: 8 }));
    button('projectDispatch.publish').click(); await flushed(); expect(p.publish).not.toHaveBeenCalled();
  });
  it('publish facade pins exact JSON and the canonical v4 publication path', async () => {
    const post = vi.fn(); const api = createProjectEndpoints({ get: vi.fn(), post, request: vi.fn() } as unknown as EndpointPorts);
    const body = { projectPath: '/project', assignmentId: 'old-assignment', expectedRevision: 6,
      resultRevision: 11, expectedCheckpointSha: 'checkpoint-sha' };
    await api.publishAssignment(body);
    expect(post).toHaveBeenCalledWith('/api/v4/project/coordination/assignments/publish', body, { onError: 'throw' });
  });
});


const dependencyInput = (id: string) => document.querySelector<HTMLInputElement>(`[name=dependency][data-assignment-id="${id}"]`)!;
function selectDependency(id: string, checked = true) {
  const input = dependencyInput(id); input.checked = checked; input.dispatchEvent(new Event('change'));
}
describe('dependency drawer integration', () => {
  it('selection is local, resets consent, preserves stale pins on refresh and sends only exact explicit references', async () => {
    const receipt = fileResult(); const { p } = await showReceipt(receipt);
    fill(); consent(); selectDependency('old-assignment');
    expect(control('consent').checked).toBe(false); expect(button('projectDispatch.submit').disabled).toBe(true);
    expect(p.grant).not.toHaveBeenCalled(); expect(p.review).not.toHaveBeenCalled(); expect(p.publish).not.toHaveBeenCalled();
    const newer = structuredClone(receipt); newer.grantRevision = 7; newer.delivery!.checkpointSha = 'new-checkpoint';
    p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [newer] })); button('projectDispatch.refresh').click(); await flushed();
    expect(document.body.textContent).toContain('projectDispatch.dependencyStale'); expect(dependencyInput('old-assignment').checked).toBe(true);
    consent(); submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(vi.mocked(p.grant).mock.calls[0][0].dependencies).toEqual([{ assignmentId: 'old-assignment', grantRevision: 6, resultRevision: 11, checkpointSha: 'checkpoint-sha' }]);
    await flushed(); expect(control('consent').checked).toBe(false); expect(dependencyInput('old-assignment').checked).toBe(false);
  });
  it('mismatched grant ACK and command receipts retain frozen dependencies across reopen until an exact ordered receipt proves the request', async () => {
    const receipt = fileResult(); const second = structuredClone(receipt); second.assignmentId = 'second'; second.request.profile = undefined;
    const { p, scope, root } = setup(); p.list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [receipt, second] }));
    await flushed(); button('projectDispatch.refresh').click(); await flushed(); fill(); selectDependency('old-assignment'); selectDependency('second'); consent();
    p.grant = vi.fn(async (request: ProjectDispatchRequest) => view({ ...request, dependencies: request.dependencies!.map((pin, index) => index ? pin : { ...pin, checkpointSha: 'wrong' }) }));
    submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce()); await flushed();
    const [request, key] = vi.mocked(p.grant).mock.calls[0];
    expect(button('projectDispatch.retryExact')).toBeDefined(); root.remove(); document.body.append(mount(p, scope)); await flushed();
    expect(dependencyInput('old-assignment').checked).toBe(true); expect(dependencyInput('old-assignment').disabled).toBe(true);
    selectDependency('old-assignment', false); expect(dependencyInput('old-assignment').checked).toBe(true);
    const mismatches = [undefined, request.dependencies!.slice(0, 1), [...request.dependencies!].reverse(),
      request.dependencies!.map((pin, index) => index ? pin : { ...pin, resultRevision: 12 }),
      request.dependencies!.map((pin, index) => index ? pin : { ...pin, grantRevision: 7 }),
      request.dependencies!.map((pin, index) => index ? pin : { ...pin, checkpointSha: undefined })];
    for (const dependencies of mismatches) {
      p.list = vi.fn(async (_path, command) => ({ olderCursor: null, newerCursor: null, assignments: command ? [view({ ...request, dependencies })] : [] }));
      button('projectDispatch.refresh').click(); await flushed(); expect(button('projectDispatch.retryExact')).toBeDefined();
      expect(control('brief').disabled).toBe(true);
    }
    p.grant = vi.fn(async retained => view(retained)); submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(vi.mocked(p.grant).mock.calls[0]).toEqual([request, key]); await flushed(); expect(control('brief').disabled).toBe(false);
  });
  it('fresh rework clears old dependency pins and does not send a request', async () => {
    const receipt = fileResult(); receipt.review = { decision: 'rework', resultRevision: 11, note: '' };
    receipt.request.dependencies = [{ assignmentId: 'old-dep', grantRevision: 2, resultRevision: 3 }];
    const { p } = await showReceipt(receipt); fill(); selectDependency('old-assignment'); consent();
    button('projectDispatch.prepareRework').click();
    expect(dependencyInput('old-assignment').checked).toBe(false); expect(control('consent').checked).toBe(false);
    expect(document.querySelector('[data-testid=dispatch-dependencies]')!.textContent).toContain('"count":0'); expect(p.grant).not.toHaveBeenCalled();
    fillFiles(); consent(); submit(); await vi.waitFor(() => expect(p.grant).toHaveBeenCalledOnce());
    expect(vi.mocked(p.grant).mock.calls[0][0].dependencies).toBeUndefined();
  });
  it('late candidate reads and stale selection events cannot change the scoped draft', async () => {
    const { p, stale } = setup(); await flushed(); let resolve!: (page: { olderCursor: null, newerCursor: null, assignments: ProjectDispatchView[] }) => void;
    p.list = vi.fn(() => new Promise<{ olderCursor: null, newerCursor: null, assignments: ProjectDispatchView[] }>(done => { resolve = done; })); button('projectDispatch.refresh').click(); stale();
    resolve({ olderCursor: null, newerCursor: null, assignments: [fileResult()] }); await flushed(); expect(dependencyInput('old-assignment')).toBeNull(); expect(p.grant).not.toHaveBeenCalled();
  });
});
