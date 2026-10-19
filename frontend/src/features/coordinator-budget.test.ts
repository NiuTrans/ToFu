// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { createCoordinatorBudget } from './coordinator-budget';
import type { CoordinatorBudgetState } from '../api/project-dispatch.generated';
vi.mock('../i18n', () => ({ t: (key: string, values?: unknown) => key + (values ? JSON.stringify(values) : '') }));

function setup() {
  const state: CoordinatorBudgetState = { projectPath: '/project', revision: 2,
    settings: { maxTokens: 1000, maxModelRequests: 10, maxToolCalls: 20, durationSeconds: 7200 },
    request: { id: 'request', sourceConversationId: 'coord', sourceTurnId: 'human', createdAt: 1000,
      policy: { maxTokens: 100, maxModelRequests: 4, maxToolCalls: 5, deadline: 10000 },
      consumption: { observedTokens: 10, heldTokens: 90, remainingTokens: 10,
        modelRequests: 1, heldModelRequests: 3, remainingModelRequests: 1,
        toolCalls: 2, heldToolCalls: 5, remainingToolCalls: 0, usageComplete: false } } };
  const ports = { read: vi.fn(), update: vi.fn(async request => ({ ...state, revision: 3, settings: request.settings })) };
  const scope = { path: '/project', binding: { projectPath: '/project', conversationId: 'coord', revision: 7,
    availability: 'available' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false as const } }, valid: () => true };
  const view = createCoordinatorBudget(ports, scope);
  document.body.replaceChildren(view.summary, view.settings); view.update(state);
  return { view, state, ports, scope };
}
describe('Coordinator request allowance', () => {
  it('distinguishes actual usage, reservations and future settings without writes', () => {
    const { view, ports } = setup();
    expect(view.summary.textContent).toContain('"used":"10","total":"100"');
    expect(view.summary.textContent).toContain('"reserved":"80","remaining":"10"');
    expect(view.summary.textContent).toContain('coordinatorBudget.pending');
    expect(view.summary.querySelector('[role="progressbar"]')!.getAttribute('aria-valuenow')).toBe('90');
    expect(view.settings.querySelector<HTMLInputElement>('[name="maxTokens"]')!.value).toBe('1000');
    expect(ports.update).not.toHaveBeenCalled();
  });
  it('keeps edited defaults and their CAS revision during passive refresh', async () => {
    const { view, state, ports } = setup();
    const input = view.settings.querySelector<HTMLInputElement>('[name="maxTokens"]')!;
    input.value = '2500'; input.dispatchEvent(new Event('input', { bubbles: true }));
    view.update({ ...state, revision: 99, settings: { ...state.settings, maxTokens: 5 } });
    expect(input.value).toBe('2500');
    view.settings.dispatchEvent(new Event('submit', { cancelable: true }));
    await vi.waitFor(() => expect(ports.update).toHaveBeenCalledOnce());
    expect(ports.update).toHaveBeenCalledWith({ path: '/project', conversationId: 'coord', bindingRevision: 7,
      expectedRevision: 2, settings: { ...state.settings, maxTokens: 2500 } });
    await vi.waitFor(() => expect(view.settings.textContent).toContain('coordinatorBudget.saved'));
    expect(view.summary.textContent).toContain('"used":"10","total":"100"');
  });
  it('preserves input after failed save and refuses detached scopes', async () => {
    const { view, ports, scope } = setup();
    ports.update.mockRejectedValueOnce(new Error('Conflicting budget settings'));
    view.settings.dispatchEvent(new Event('submit', { cancelable: true }));
    await vi.waitFor(() => expect(view.settings.textContent).toContain('Conflicting budget settings'));
    expect(view.settings.querySelector('button')!.disabled).toBe(false);
    scope.valid = () => false;
    view.settings.dispatchEvent(new Event('submit', { cancelable: true }));
    expect(ports.update).toHaveBeenCalledOnce();
  });
});
