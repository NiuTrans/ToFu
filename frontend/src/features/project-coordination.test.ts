// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
import { openProjectCoordination, projectCoordinationWorkGroups, type ProjectCoordinationPorts } from './project-coordination';

beforeEach(() => {
  document.body.replaceChildren();
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event('close')); };
});
function ports(): ProjectCoordinationPorts {
  return {
    path: '/project', currentConversationId: 'worker', currentPath: () => '/project',
    binding: vi.fn(async () => ({ projectPath: '/project', conversationId: 'coord', revision: 1,
      availability: 'available' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false as const } })),
    board: vi.fn(async () => ({ active: [{ id: 'work', conversationId: 'worker', title: '<unsafe>', status: 'active' as const }],
      recentOutcomes: [{ id: 'done', title: 'Done', status: 'completed' as const }] })),
    bind: vi.fn(), navigate: vi.fn(), openEvidence: vi.fn(),
  };
}
async function rendered() {
  await vi.waitFor(() => expect(document.body.textContent).toContain('projectCoordination.openCoordinator'));
}
describe('project workspace', () => {
  it('projects actual status without treating completion as delivery', () => {
    const groups = projectCoordinationWorkGroups([{ id: 'bad', status: 'completed' }], [{ id: 'bad', status: 'active' }]);
    expect(groups).toEqual({ active: [], recent: [] });
  });
  it('escapes text and navigates in the ordinary canvas', async () => {
    const p = ports(); const run = openProjectCoordination(p);
    await rendered();
    expect(document.body.textContent).toContain('<unsafe>');
    expect(document.body.innerHTML).not.toContain('<unsafe>');
    expect(document.body.textContent).toContain('projectCoordination.deliveryCaveat');
    const button = [...document.querySelectorAll('button')].find(n => n.textContent === 'projectCoordination.openWorker')!;
    button.click(); await run;
    expect(p.navigate).toHaveBeenCalledWith('worker');
    expect(document.querySelector('dialog')).toBeNull();
  });
  it('closes a stale project response without rendering another scope', async () => {
    const p = ports(); let resolve!: (value: Awaited<ReturnType<typeof p.binding>>) => void;
    const value = await p.binding('/project');
    p.binding = () => new Promise(r => { resolve = r; });
    const run = openProjectCoordination(p);
    p.currentPath = () => '/other'; resolve(value); await run;
    expect(document.querySelector('dialog')).toBeNull();
    expect(p.navigate).not.toHaveBeenCalled();
  });
  it('shows child messaging only from the server capability, not coordinator binding', async () => {
    const p = ports(); const binding = await p.binding('/project');
    p.binding = async () => ({ ...binding, capabilities: { ...binding.capabilities, steerSubagent: true } });
    const run = openProjectCoordination(p); await rendered();
    expect(document.body.textContent).toContain('projectCoordination.capabilityBodyChildMessages');
    expect(document.body.textContent).not.toMatch(/projectCoordination\.capabilityBody(?!ChildMessages)/);
    document.querySelector('dialog')!.close(); await run;
  });
  it('shows an unavailable board instead of a fabricated empty roster', async () => {
    const p = ports(); p.board = async () => null;
    const run = openProjectCoordination(p); await rendered();
    expect(document.body.textContent).toContain('projectCoordination.boardUnavailable');
    expect(document.body.textContent).not.toContain('projectCoordination.empty');
    document.querySelector('dialog')!.close(); await run;
  });
});
