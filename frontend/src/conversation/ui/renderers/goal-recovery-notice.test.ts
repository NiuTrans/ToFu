// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderGoalRecoveryNotice, disposeGoalRecoveryNotice } from './goal-recovery-notice';
import { selectAutopilotRunNotices } from '../../presentation/view-model/live-turns';
import { createOrchestrationTaskRequestClient } from '../../../features/orchestration/task-request';
import type { ClassicConversationRendererPorts } from './ports';

afterEach(() => { document.body.replaceChildren(); });
const value = { runId: 'goal-source', reason: 'worker_lost' as const, unsent: false, canRequestRecovery: true };
const ports = (resumeGoalRun: ClassicConversationRendererPorts['resumeGoalRun']): ClassicConversationRendererPorts => ({
  renderSafeMarkdownHtml: text => text, resumeGoalRun,
});
const mounted = () => { const node = document.createElement('div'); document.body.append(node); return node; };

it('joins a first-worker interruption by Goal identity, gates later human input and ignores ordinary attempts', () => {
  const source = { turnId: 'worker', actor: 'assistant', runId: 'attempt-not-goal',
    status: 'interrupted', settlement: { cause: 'server_restart' },
    projection: { orchestration: { goalRunId: 'goal-source' } } };
  const state = { laneOrder: { main: ['worker'] }, turnsById: { worker: source } };
  const select = () => selectAutopilotRunNotices(state as never, {} as never).get('worker')?.[0].value;
  expect(select()).toMatchObject({ runId: 'goal-source', canRequestRecovery: true });
  Object.assign(state.turnsById, { human: { turnId: 'human', actor: 'human', projection: {} } });
  state.laneOrder.main.push('human');
  expect(select()?.canRequestRecovery).toBe(false);
  // A newly admitted virtual continuation may not have begun its first phase.
  Object.assign(state.turnsById, { human: { turnId: 'human', actor: 'virtual_user', projection: {} } });
  expect(select()).toMatchObject({ runId: 'goal-source', canRequestRecovery: false });
  source.projection.orchestration.goalRunId = '';
  expect(select()).toBeUndefined();
});

it('accepts a resume successor without weakening exact identity checks for task reads', async () => {
  const body = { ok: true, status: 200, data: { ok: true, run: { id: 'goal-successor', status: 'running' } } };
  const api = { taskResume: vi.fn(async () => body), taskGetResult: vi.fn(async () => body) };
  const client = createOrchestrationTaskRequestClient({ api });
  expect(await client.resume('goal-source')).toMatchObject({ ok: true, runId: 'goal-successor' });
  // Read method name comes from the same generated endpoint catalogue.
  const { ORCHESTRATION_REQUEST_CONTRACTS } = await import('../../../features/orchestration/request-contract');
  Object.assign(api, { [ORCHESTRATION_REQUEST_CONTRACTS['task-read'].resultMethod]: async () => body });
  expect(await client.get('goal-source')).toMatchObject({ ok: false });
});

describe('request lifecycle', () => {
  it('keeps one request and focus through rerender, distinguishes acceptance from completion', async () => {
    let finish!: (result: { ok: boolean; status: number; runId: string }) => void;
    const request = vi.fn(() => new Promise<{ ok: boolean; status: number; runId: string }>(resolve => { finish = resolve; }));
    const node = mounted(); const injected = ports(request);
    renderGoalRecoveryNotice(node, value, injected);
    const button = node.querySelector('button')!; button.focus(); button.click(); button.click();
    renderGoalRecoveryNotice(node, value, injected);
    expect(request).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(button);
    finish({ ok: true, status: 200, runId: 'goal-successor' });
    await vi.waitFor(() => expect(node.querySelector('[data-goal-recovery="accepted"]')).not.toBeNull());
    expect(document.activeElement).toBe(button);
    button.click(); expect(request).toHaveBeenCalledTimes(1);
    expect(node.textContent).toContain('Follow its progress');
  });
  it('retains unknown delivery without automatic retries and ignores replies after disposal', async () => {
    const node = mounted(); const request = vi.fn().mockRejectedValue(new Error('connection lost'));
    renderGoalRecoveryNotice(node, value, ports(request)); node.querySelector('button')!.click();
    await vi.waitFor(() => expect(node.querySelector('[data-goal-recovery="unknown"]')).not.toBeNull());
    expect(request).toHaveBeenCalledTimes(1);
    let finish!: (result: { ok: boolean; status: number; runId: string }) => void;
    const pending = ports(() => new Promise(resolve => { finish = resolve; }));
    renderGoalRecoveryNotice(node, value, pending); node.querySelector('button')!.click();
    disposeGoalRecoveryNotice(node); node.textContent = 'Different conversation';
    finish({ ok: true, status: 200, runId: 'goal-successor' });
    await Promise.resolve(); await Promise.resolve();
    expect(node.textContent).toBe('Different conversation');
  });
  it('renders server conflict as text and prevents requests from historical notices', async () => {
    const request = vi.fn(async () => ({ ok: false, status: 409, error: '<script>unknown effect</script>' }));
    const node = mounted();
    renderGoalRecoveryNotice(node, value, ports(request)); node.querySelector('button')!.click();
    await vi.waitFor(() => expect(node.querySelector('[data-goal-recovery="refused"]')).not.toBeNull());
    expect(node.querySelector('script')).toBeNull(); expect(node.textContent).toContain('<script>unknown effect</script>');
    renderGoalRecoveryNotice(node, { ...value, canRequestRecovery: false }, ports(request));
    expect(node.querySelector('button')).toBeNull();
  });
});
