// @vitest-environment jsdom
/** Local dependency pins and conservative server projections. No paid/native calls. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ProjectDispatchView, ProjectDispatchDependencyState } from '../api/project-dispatch.generated';
import { createDispatchDependencies, dependencyProjection, sameDependencies } from './project-dispatch-dependencies';
vi.mock('../i18n', () => ({ t: (key: string, params?: Record<string, unknown>) => `${key}${params ? ':' + JSON.stringify(params) : ''}` }));
beforeEach(() => document.body.replaceChildren());
const candidate = (assignmentId = 'consult'): ProjectDispatchView => ({ assignmentId, grantRevision: 2, revoked: false,
  request: { projectPath: '/project', coordinatorId: 'coord', recipientId: 'worker', brief: '<unsafe>', model: 'mock',
    authorizationSource: { conversationId: 'coord', turnId: 'human', projectionRevision: 1 },
    budget: { maxTokens: 1, maxModelRequests: 1, deadline: 2000000000 } },
  execution: { status: 'completed', resultRevision: 7, result: {}, turnId: 'result', taskId: 'task', attemptId: 'attempt' },
  review: null, canonicalDelivery: false });
function fixture() {
  const changed = vi.fn(); let valid = true;
  const draft = createDispatchDependencies('/project', () => valid, changed); document.body.append(draft.el);
  return { draft, changed, stale: () => { valid = false; } };
}
const input = (id: string) => document.querySelector<HTMLInputElement>(`[data-assignment-id="${id}"]`)!;
const choose = (id: string, checked = true) => { input(id).checked = checked; input(id).dispatchEvent(new Event('change')); };
describe('dependency draft', () => {
  it('only completed same-project candidates; consult has no SHA and files require displayed checkpoint without delivery inference', () => {
    const { draft, changed } = fixture(); const consult = candidate();
    consult.delivery = { taskId: 'task', status: 'delivered', reason: '', checkpointSha: 'ignore-for-consult', targetSha: 'target' };
    const files = candidate('files'); files.request.profile = { mode: 'files', tools: ['read_files'], readPaths: ['src'], writePaths: [], maxToolCalls: 1 };
    const missing = structuredClone(files); missing.assignmentId = 'missing'; missing.canonicalDelivery = true;
    files.delivery = { taskId: 'task', status: 'blocked', reason: 'held', checkpointSha: 'shown-checkpoint', targetSha: '' };
    const foreign = candidate('foreign'); foreign.request.projectPath = '/other';
    const running = candidate('running'); running.execution!.status = 'running';
    const revoked = candidate('revoked'); revoked.revoked = true;
    draft.update([consult, files, missing, foreign, running, revoked]);
    expect(changed).not.toHaveBeenCalled(); expect(input('foreign')).toBeNull(); expect(input('running')).toBeNull(); expect(input('revoked')).toBeNull();
    expect(document.querySelector<HTMLInputElement>('[name=dependency-unavailable]')!.disabled).toBe(true);
    choose('consult'); choose('files');
    expect(draft.references()).toEqual([{ assignmentId: 'consult', grantRevision: 2, resultRevision: 7 },
      { assignmentId: 'files', grantRevision: 2, resultRevision: 7, checkpointSha: 'shown-checkpoint' }]);
    expect(document.body.textContent).toContain('shown-checkpoint'); expect(document.body.innerHTML).not.toContain('<unsafe>');
    expect(changed).toHaveBeenCalledTimes(2);
  });
  it('caps at eight even for synthetic change events and releases space only after explicit removal', () => {
    const { draft, changed } = fixture(); draft.update(Array.from({ length: 9 }, (_, n) => candidate(`a${n}`)));
    for (let n = 0; n < 8; n++) choose(`a${n}`);
    expect(input('a8').disabled).toBe(true); choose('a8'); expect(draft.references()).toHaveLength(8);
    expect(changed).toHaveBeenCalledTimes(8); choose('a0', false); choose('a8'); expect(draft.references()).toHaveLength(8);
    expect(draft.references()[7].assignmentId).toBe('a8');
  });
  it('preserves old pins through grant/result/checkpoint changes, disappearance and failed reads; never silently upgrades', () => {
    const { draft, changed } = fixture(); draft.update([candidate()]); choose('consult');
    const original = draft.references(); const newer = candidate(); newer.grantRevision = 3; newer.execution!.resultRevision = 8;
    draft.update([newer]); expect(draft.references()).toEqual(original);
    expect(document.body.textContent).toContain('projectDispatch.dependencyStale');
    expect(document.querySelectorAll('[data-assignment-id=consult]')).toHaveLength(1);
    draft.update([]); draft.update(null); expect(draft.references()).toEqual(original); expect(changed).toHaveBeenCalledOnce();
    draft.update([newer]); choose('consult', false); choose('consult');
    expect(draft.references()).toEqual([{ assignmentId: 'consult', grantRevision: 3, resultRevision: 8 }]);
    const file = candidate('file'); file.request.profile = { mode: 'files', tools: ['read_files'], readPaths: ['src'], writePaths: [], maxToolCalls: 1 };
    file.delivery = { taskId: 'task', status: 'blocked', reason: '', checkpointSha: 'old-sha', targetSha: '' };
    draft.restore(); draft.update([file]); choose('file'); file.delivery.checkpointSha = 'new-sha'; draft.update([file]);
    expect(draft.references()[0].checkpointSha).toBe('old-sha'); expect(document.body.textContent).toContain('projectDispatch.dependencyStale');
  });
  it('adds semantic breaks to full SHA references without truncating or changing text', () => {
    const { draft } = fixture(); const file = candidate('long-assignment-identifier');
    file.request.profile = { mode: 'files', tools: ['read_files'], readPaths: ['src'], writePaths: [], maxToolCalls: 1 };
    const sha = '0123456789abcdef0123456789abcdef01234567';
    file.delivery = { taskId: 'task', status: 'blocked', reason: '', checkpointSha: sha, targetSha: '' };
    draft.update([file]); expect(document.body.textContent).toContain(sha); expect(document.querySelector('wbr')).not.toBeNull();
    choose(file.assignmentId); expect(draft.references()[0].checkpointSha).toBe(sha);
  });
  it('keeps keyboard focus on the exact checkbox through selection and refresh', () => {
    const { draft } = fixture(); draft.update([candidate()]); input('consult').focus(); choose('consult');
    expect(document.activeElement).toBe(input('consult')); draft.update([candidate()]);
    expect(document.activeElement).toBe(input('consult')); choose('consult', false);
    expect(document.activeElement).toBe(input('consult'));
  });
  it('late scopes and frozen drafts cannot be changed; restores copy references without change callbacks', () => {
    const { draft, changed, stale } = fixture(); draft.update([candidate()]); stale(); choose('consult'); expect(draft.references()).toEqual([]);
    const pins = [{ assignmentId: 'missing', grantRevision: 2, resultRevision: 7 }]; draft.restore(pins); pins[0].resultRevision = 8;
    draft.disable(true); choose('missing', false); expect(draft.references()[0].resultRevision).toBe(7); expect(changed).not.toHaveBeenCalled();
  });
});
describe('exact references and backend-only projection', () => {
  it('ACK identity includes all references in order and optional checkpoint without substitution', () => {
    const a = [{ assignmentId: 'a', grantRevision: 2, resultRevision: 7, checkpointSha: 'sha' }, { assignmentId: 'b', grantRevision: 3, resultRevision: 9 }];
    expect(sameDependencies(a, structuredClone(a))).toBe(true); expect(sameDependencies(a, [...a].reverse())).toBe(false);
    for (const change of [{ assignmentId: 'other' }, { grantRevision: 3 }, { resultRevision: 8 }, { checkpointSha: 'new-sha' }, { checkpointSha: undefined }]) {
      expect(sameDependencies(a, [{ ...a[0], ...change }, a[1]])).toBe(false);
    }
    expect(sameDependencies(a, a.slice(0, 1))).toBe(false); expect(sameDependencies(undefined, [])).toBe(false);
  });
  it('absence and mismatched backend state remain unknown despite completed, accepted, delivered candidate', () => {
    const receipt = candidate(); receipt.review = { decision: 'accept', resultRevision: 7, note: '' }; receipt.canonicalDelivery = true;
    receipt.request.dependencies = [{ assignmentId: 'upstream', grantRevision: 2, resultRevision: 7 }];
    expect(dependencyProjection(receipt)!.textContent).toContain('dependencyStatusUnknown');
    receipt.dependencyStates = [{ reference: { ...receipt.request.dependencies[0], resultRevision: 8 }, status: 'ready', reason: 'ready' }];
    expect(dependencyProjection(receipt)!.textContent).toContain('dependencyStatusUnknown');
    expect(dependencyProjection(receipt)!.textContent).not.toContain('dependencyStatusReady');
  });
  it('renders all declared server states and localized reason keys without task state inference', () => {
    const receipt = candidate(); const reference = { assignmentId: 'upstream', grantRevision: 2, resultRevision: 7 }; receipt.request.dependencies = [reference];
    const reasons: ProjectDispatchDependencyState['reason'][] = ['ready', 'review_required', 'delivery_required', 'revoked', 'scope_changed', 'result_changed', 'review_rejected', 'stopped', 'evidence_unavailable', 'dependency_changed', 'checkpoint_changed'];
    for (const reason of reasons) {
      receipt.dependencyStates = [{ reference, status: reason === 'ready' ? 'ready' : reason === 'review_required' ? 'waiting_review' : reason === 'delivery_required' ? 'waiting_delivery' : reason === 'evidence_unavailable' ? 'unknown' : 'invalid', reason }];
      expect(dependencyProjection(receipt)!.textContent).toContain('projectDispatch.dependencyReason');
      expect(dependencyProjection(receipt)!.textContent).not.toContain('undefined');
    }
    receipt.request.dependencies = undefined; expect(dependencyProjection(receipt)).toBeNull();
  });
});
