// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest';
import type { ProjectDispatchPage } from '../api/project-dispatch.generated';
import type { ProjectDispatchPorts } from './project-dispatch';

vi.mock('../i18n', async () => {
  const { default: catalog } = await import('../i18n/locales/en.json');
  return { t: (key: keyof typeof catalog, params: Record<string, unknown> = {}) => catalog[key].replace(/\{(\w+)\}/g, (_, name) => String(params[name] ?? '')) };
});
const binding = { projectPath: '/p', conversationId: 'coord', revision: 1, availability: 'available' as const,
  capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false as const } };
const scope = () => ({ path: '/p', conversationId: 'coord', binding, valid: () => true, readBinding: vi.fn(async () => binding) });
const grant = { assignmentId: 'a', grantRevision: 7, revoked: false, request: { projectPath: '/p', coordinatorId: 'coord',
  recipientId: 'worker', authorizationSource: { conversationId: 'coord', turnId: 'human', projectionRevision: 3 },
  brief: '<script>untrusted</script>', model: 'model', budget: { maxTokens: 100, maxModelRequests: 2, deadline: 9999999999 } },
  execution: null, review: null, canonicalDelivery: false };
const dispatch = () => ({ recipients: () => [{ id: 'worker', title: 'Not a model hint' }],
  humanTurns: () => [{ actor: 'human' as const, turnId: 'human', projectionRevision: 3 }],
  list: vi.fn(async (..._args: Parameters<ProjectDispatchPorts['list']>): Promise<ProjectDispatchPage> => ({ olderCursor: null, newerCursor: null, assignments: [grant] })),
  commandId: vi.fn(() => 'exact-key'), grant: vi.fn(), dispatch: vi.fn(), revoke: vi.fn(), review: vi.fn(), publish: vi.fn() });
const change = { changeId: 'c', request: { projectPath: '/p', coordinatorId: 'coord',
  authorizationSource: { conversationId: 'coord', turnId: 'human', projectionRevision: 3 }, kind: 'shrink' as const,
  note: '', targets: [{ assignmentId: 'a', grantRevision: 7 }] }, createdAt: 10,
  revocations: [{ assignmentId: 'a', grantRevision: 7, revokedRevision: 8, recipientId: 'worker', execution: null,
    cancellation: { state: 'signaled' as const, status: 'cancelling', error: '' } }], notifications: [], authorityRevoked: true as const, stopConfirmed: false };
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function value(root: HTMLElement, name: string, input: string) {
  const node = root.querySelector<HTMLInputElement>(`[name="${name}"]`)!; node.value = input; node.dispatchEvent(new Event('change', { bubbles: true }));
}
function check(root: HTMLElement, name: string) {
  const node = root.querySelector<HTMLInputElement>(`[name="${name}"]`)!; node.checked = true; node.dispatchEvent(new Event('change', { bubbles: true }));
}
const button = (root: HTMLElement, starts: string) => [...root.querySelectorAll<HTMLButtonElement>('button')].find(node => node.textContent!.startsWith(starts))!;
beforeEach(() => { vi.resetModules(); document.body.replaceChildren(); });
it('reads only on mount, pins revisions and clears consent on edits; cancellation is not Stop', async () => {
  const { createProjectScopeChanges } = await import('./project-scope-changes');
  const ports = { list: vi.fn(async () => [change]), apply: vi.fn(async () => change), retry: vi.fn() };
  const root = createProjectScopeChanges(ports, dispatch(), scope()); await settle();
  expect(ports.apply).not.toHaveBeenCalled(); expect(root.querySelector('script')).toBeNull();
  value(root, 'scopeSource', 'human'); check(root, 'scopeTarget'); check(root, 'scopeConsent');
  expect(button(root, 'Revoke selected').disabled).toBe(false);
  value(root, 'scopeNote', 'shrink'); expect(button(root, 'Revoke selected').disabled).toBe(true);
  check(root, 'scopeConsent'); button(root, 'Revoke selected').click(); await settle();
  expect(ports.apply).toHaveBeenCalledWith(expect.objectContaining({ targets: [{ assignmentId: 'a', grantRevision: 7 }] }), 'exact-key');
  expect(root.textContent).toContain('Termination is not yet confirmed');
  expect(root.textContent).not.toContain('Termination confirmed by');
});
it('retries exact lost ACK after close and suppresses delayed navigation replies', async () => {
  const { createProjectScopeChanges } = await import('./project-scope-changes');
  const ports = { list: vi.fn(async () => []), apply: vi.fn().mockRejectedValueOnce(new Error('lost ACK')).mockResolvedValue(change), retry: vi.fn() };
  let alive = true; const current = scope(); current.valid = () => alive;
  const first = createProjectScopeChanges(ports, dispatch(), current); await settle();
  value(first, 'scopeSource', 'human'); check(first, 'scopeTarget'); check(first, 'scopeConsent');
  button(first, 'Revoke selected').click(); await settle(); const [body, key] = ports.apply.mock.calls[0]; alive = false;
  const second = createProjectScopeChanges(ports, dispatch(), scope()); await settle();
  button(second, 'Retry the exact').click(); await settle(); expect(ports.apply.mock.calls[1]).toEqual([body, key]);
  let resolve!: (rows: never[]) => void; const delayed = scope(); delayed.valid = () => alive;
  alive = true; const third = createProjectScopeChanges({ ...ports, list: vi.fn(() => new Promise<never[]>(r => { resolve = r; })) }, dispatch(), delayed);
  alive = false; resolve([]); await settle(); expect(third.querySelector('[name="scopeTarget"]')).toBeNull();
});
it('requires finite explicit terms and model, with no inference from worker labels', async () => {
  const { createProjectResponsibilities } = await import('./project-responsibilities');
  const ports = { list: vi.fn(async () => []), create: vi.fn(async body => ({ responsibilityId: 'resp', revision: 1, owner: 1, tenant: null,
    projectPath: '/p', request: body, state: 'active' as const, automationId: 'auto', runsReserved: 0, tokensReserved: 0, occurrences: [] })),
    pause: vi.fn(), cancel: vi.fn(), reconcile: vi.fn() };
  const root = createProjectResponsibilities(ports, dispatch(), scope()); await settle(); expect(ports.create).not.toHaveBeenCalled();
  for (const [name, input] of Object.entries({ responsibilityName: 'Finite review', responsibilitySource: 'human', responsibilityRecipient: 'worker',
    responsibilityBrief: 'Review', responsibilityMaxRuns: '3', responsibilityTotalTokens: '600', responsibilityPerRunTokens: '200',
    responsibilityPerRunRequests: '2', responsibilityPerRunSeconds: '60', responsibilityDeadline: '2099-01-01T00:00:00Z',
    responsibilitySchedule: '0 9 * * 1', responsibilityTimezone: 'UTC' })) value(root, name, input);
  check(root, 'responsibilityConsent'); button(root, 'Create finite').click(); await settle(); expect(ports.create).not.toHaveBeenCalled();
  value(root, 'responsibilityModel', 'explicit-model'); expect(button(root, 'Create finite').disabled).toBe(true);
  check(root, 'responsibilityConsent'); button(root, 'Create finite').click(); await settle();
  const body = ports.create.mock.calls[0][0]; expect(body.allowance.maxRuns).toBe(3);
  expect(body.assignment.budget.deadline).toBe(body.allowance.deadline);
  expect(body.assignment.profile).toEqual({ mode: 'consult', tools: [], readPaths: [], writePaths: [], maxToolCalls: 0 });
});
it('opens exact content as text, rejects silently newer data and disables stale refs', async () => {
  const { createProjectSharedFiles } = await import('./project-shared-files');
  const entry = { id: 'file', revision: 4, path: 'notes/decision.txt', purpose: '<img src=x>', digest: 'a'.repeat(64), byteLength: 20,
    source: { kind: 'human' as const, subject: 'human-owner', authorizationSource: null, assignmentId: null, grantRevision: null, taskId: null },
    checkpointSha: null, knowledgeState: 'candidate' as const, status: 'current' as const, observedDigest: null, replacementPath: null, updatedAt: 1 };
  const ports = { list: vi.fn(async () => ({ projectPath: '/p', revision: 1, entries: [entry, { ...entry, id: 'stale', status: 'changed' as const }], nextCursor: null })),
    read: vi.fn(async () => ({ reference: { id: entry.id, revision: entry.revision, digest: entry.digest }, path: entry.path,
      digest: entry.digest, byteLength: 20, trust: 'untrusted_data' as const, content: '<script>window.pwned = true</script>' })) };
  const root = createProjectSharedFiles(ports, scope()); await settle();
  expect(root.querySelector('img')).toBeNull(); expect(root.textContent).toContain('Candidate · not adopted');
  const opens = [...root.querySelectorAll<HTMLButtonElement>('button')].filter(node => node.textContent!.startsWith('Open exact'));
  expect(opens[1].disabled).toBe(true); opens[0].click(); await settle();
  expect(ports.read).toHaveBeenCalledWith('/p', { projectPath: '/p', reference: { id: 'file', revision: 4, digest: entry.digest } });
  expect(root.querySelector('pre')?.textContent).toContain('<script>'); expect(root.querySelector('script')).toBeNull();
  ports.read.mockResolvedValue({ ...(await ports.read.mock.results[0].value), reference: { id: 'file', revision: 5, digest: entry.digest } });
  opens[0].click(); await settle(); expect(root.querySelector('pre')).toBeNull(); expect(root.textContent).toContain('no longer matches');
});

it('can withdraw existing subscriptions with no current grants, with separate consent and confirmation', async () => {
  const { createProjectScopeChanges } = await import('./project-scope-changes');
  const ports = { list: vi.fn(async () => []), apply: vi.fn(async () => ({ ...change,
    request: { ...change.request, kind: 'stop_project' as const, targets: [] }, revocations: [], stopConfirmed: true })), retry: vi.fn() };
  const d = dispatch(); d.list.mockResolvedValue({ olderCursor: null, newerCursor: null, assignments: [] });
  const root = createProjectScopeChanges(ports, d, scope()); await settle();
  value(root, 'scopeSource', 'human'); check(root, 'scopeStopConsent');
  expect(button(root, 'Stop project').disabled).toBe(true);
  check(root, 'scopeStopConfirm'); expect(button(root, 'Stop project').disabled).toBe(false);
  button(root, 'Stop project').click(); await settle();
  expect(ports.apply).toHaveBeenCalledWith(expect.objectContaining({ kind: 'stop_project', targets: [] }), 'exact-key');
});

it('pins every page before enabling Stop and refuses an incomplete target set', async () => {
  const { createProjectScopeChanges } = await import('./project-scope-changes');
  const ports = { list: vi.fn(async () => []), apply: vi.fn(async () => change), retry: vi.fn() };
  const d = dispatch();
  d.list.mockResolvedValueOnce({ assignments: [grant], olderCursor: 'older', newerCursor: null })
    .mockResolvedValueOnce({ assignments: [{ ...grant, assignmentId: 'older-grant' }], olderCursor: null, newerCursor: 'newer' });
  const root = createProjectScopeChanges(ports, d, scope()); await settle();
  value(root, 'scopeSource', 'human'); check(root, 'scopeStopConsent'); check(root, 'scopeStopConfirm');
  expect(root.querySelectorAll('[name="scopeTarget"]')).toHaveLength(2);
  button(root, 'Stop project').click(); await settle();
  expect(ports.apply).toHaveBeenCalledWith(expect.objectContaining({ targets: [
    { assignmentId: 'a', grantRevision: 7 }, { assignmentId: 'older-grant', grantRevision: 7 },
  ] }), 'exact-key');
  const many = dispatch();
  many.list.mockImplementation(async (_path, _command, options) => {
    const offset = Number(options?.cursor || 0);
    return { assignments: Array.from({ length: Math.min(32, 129 - offset) }, (_, index) => ({ ...grant, assignmentId: String(offset + index) })),
      olderCursor: offset + 32 < 129 ? String(offset + 32) : null, newerCursor: offset ? String(offset - 32) : null };
  });
  const partial = createProjectScopeChanges(ports, many, scope()); await settle();
  value(partial, 'scopeSource', 'human'); check(partial, 'scopeStopConsent'); check(partial, 'scopeStopConfirm');
  expect(button(partial, 'Stop project').disabled).toBe(true);
  expect(partial.textContent).toContain('requires a complete target set');
});
