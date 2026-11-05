// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { createCoordinatorBudget } from './coordinator-budget';
import type { CoordinatorUsageState } from '../api/project-dispatch.generated';
vi.mock('../i18n', () => ({ t: (key: string, values?: unknown) => key + (values ? JSON.stringify(values) : '') }));

function setup(state: CoordinatorUsageState | null) {
  const ports = { read: vi.fn() };
  const scope = { path: '/project', binding: { projectPath: '/project', conversationId: 'coord', revision: 7,
    availability: 'available' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false as const } }, valid: () => true };
  const view = createCoordinatorBudget(ports, scope);
  document.body.replaceChildren(view.summary); view.update(state);
  return { view, ports };
}

const usage = { tokens: 123456, modelRequests: 42, toolCalls: 300, usageComplete: true, activeWorkers: 3 };
const request = { id: 'request', sourceConversationId: 'coord', sourceTurnId: 'human', createdAt: 1000, usage };

describe('Coordinator request usage', () => {
  it('shows observed usage with no limit, progress bar or settings form', () => {
    const { view, ports } = setup({ projectPath: '/project', request });
    const text = view.summary.textContent!;
    expect(text).toContain((123456).toLocaleString());
    expect(text).toContain('42');
    expect(text).toContain('coordinatorBudget.unlimited');
    expect(text).not.toContain('coordinatorBudget.pending');
    expect(view.summary.querySelector('[role="progressbar"], form, input')).toBeNull();
    expect(ports.read).not.toHaveBeenCalled();
  });
  it('marks missing receipts as a lower bound and announces the decision default', () => {
    const { view } = setup({ projectPath: '/project', request: { ...request, usage: { ...usage, usageComplete: false },
      pendingDecision: { reportTurnId: 'report', recommendation: 'Use option A', dueAt: 5000, createdAt: 4000 } } });
    expect(view.summary.textContent).toContain('coordinatorBudget.pending');
    expect(view.summary.textContent).toContain('coordinatorBudget.decision');
  });
  it('renders empty and unavailable states', () => {
    expect(setup({ projectPath: '/project', request: null }).view.summary.textContent).toContain('coordinatorBudget.noRequest');
    expect(setup(null).view.summary.textContent).toContain('coordinatorBudget.unavailable');
  });
});
