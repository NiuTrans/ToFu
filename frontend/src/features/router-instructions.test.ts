// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRouterInstructions, type RouterInstructionPorts } from './router-instructions';
import type { RouterInstruction, RouterInstructionPage } from '../api/router.generated';
vi.mock('../i18n', () => ({ t: (key: string, values?: unknown) => key + (values ? JSON.stringify(values) : '') }));

const instruction = (id: string, state: RouterInstruction['state'], extra: Partial<RouterInstruction> = {}): RouterInstruction => ({
  version: 1, owner: 1, instructionId: id, state, routerConversationId: 'router', sourceTurnId: `turn-${id}`,
  sourceProjectionRevision: 1, text: `When someone reports a bug, reproduce it (${id})`, label: `Label ${id}`,
  projectPath: null, thread: null, createdAt: 1, cancelledAt: null, pausedAt: null, ...extra,
});

function setup(pages: (RouterInstructionPage | Error)[]) {
  const list = vi.fn(async () => {
    const page = pages.shift()!;
    if (page instanceof Error) throw page;
    return page;
  });
  const transition = (state: RouterInstruction['state']) => vi.fn(async (id: string) => instruction(id, state));
  const ports: RouterInstructionPorts = { list, pause: transition('paused'), resume: transition('active'), cancel: transition('cancelled') };
  const view = createRouterInstructions(ports);
  document.body.replaceChildren(view.el);
  // The platform queues one toggle event per change of `open`.
  const open = async () => { view.el.open = true; await settle(); };
  const row = (id: string) => view.el.querySelector<HTMLElement>(`[data-instruction-id="${id}"]`);
  const action = (id: string, name: string) => row(id)?.querySelector<HTMLButtonElement>(`[data-instruction-action="${name}"]`) ?? null;
  return { ports, list, view, open, row, action };
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0));

afterEach(() => { document.body.replaceChildren(); });

describe('Router standing instructions', () => {
  it('reads nothing until opened, then lists active and paused instructions with their scope', async () => {
    const { list, open, row, action } = setup([{ nextCursor: null, instructions: [
      instruction('a', 'active', { projectPath: '/work/app', thread: 'group-42' }), instruction('b', 'paused')] }]);
    expect(list).not.toHaveBeenCalled();
    await open();
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith(undefined, expect.any(AbortSignal));
    expect(row('a')!.textContent).toContain('Label a');
    expect(row('a')!.textContent).toContain('/work/app');
    expect(row('a')!.textContent).toContain('routerInstructions.thread{"thread":"group-42"}');
    expect(row('a')!.querySelector('.router-instruction-state')!.getAttribute('data-i18n')).toBe('routerInstructions.state.active');
    expect(row('b')!.querySelector('[data-i18n="routerInstructions.anyProject"]')).not.toBeNull();
    expect(action('a', 'pause')).not.toBeNull();
    expect(action('a', 'resume')).toBeNull();
    expect(action('b', 'resume')).not.toBeNull();
    expect(action('b', 'cancel')!.className).toContain('ui-btn--danger');
  });

  it('pauses, resumes and cancels through the owner ports, one command per instruction at a time', async () => {
    const { ports, open, row, action } = setup([{ nextCursor: null, instructions: [instruction('a', 'active'), instruction('b', 'paused')] }]);
    await open();
    action('a', 'pause')!.click();
    expect(action('a', 'cancel')!.disabled).toBe(true);
    action('a', 'cancel')!.click();
    await settle();
    expect(ports.pause).toHaveBeenCalledWith('a');
    expect(ports.cancel).not.toHaveBeenCalled();
    expect(row('a')!.dataset.state).toBe('paused');
    action('b', 'resume')!.click();
    await settle();
    expect(ports.resume).toHaveBeenCalledWith('b');
    expect(row('b')!.dataset.state).toBe('active');
    action('b', 'cancel')!.click();
    await settle();
    expect(ports.cancel).toHaveBeenCalledWith('b');
    expect(row('b')).toBeNull();
  });

  it('reports a refused transition and keeps the row as the server last stated it', async () => {
    const { ports, open, row, action, view } = setup([{ nextCursor: null, instructions: [instruction('a', 'active')] }]);
    await open();
    vi.mocked(ports.pause).mockRejectedValueOnce(new Error('A cancelled standing instruction cannot be paused'));
    action('a', 'pause')!.click();
    await settle();
    expect(view.el.querySelector('[role="status"]')!.textContent).toContain('cannot be paused');
    expect(row('a')!.dataset.state).toBe('active');
  });

  it('offers a retry after a failed read and an empty state after an empty one', async () => {
    const { list, open, view } = setup([new Error('offline'), { nextCursor: null, instructions: [] }]);
    await open();
    expect(view.el.querySelector('[role="status"]')!.textContent).toContain('routerInstructions.failed');
    view.el.querySelector<HTMLButtonElement>('[data-i18n="routerInstructions.retry"]')!.click();
    await settle();
    expect(list).toHaveBeenCalledTimes(2);
    expect(view.el.querySelector('[role="status"]')!.getAttribute('data-i18n')).toBe('routerInstructions.empty');
  });

  it('pages with the server cursor and rereads the first page on every opening', async () => {
    const { list, open, row, view } = setup([
      { nextCursor: 'c1', instructions: [instruction('a', 'active')] },
      { nextCursor: null, instructions: [instruction('b', 'paused')] },
      { nextCursor: null, instructions: [instruction('b', 'paused')] },
    ]);
    await open();
    view.el.querySelector<HTMLButtonElement>('[data-i18n="routerInstructions.more"]')!.click();
    await settle();
    expect(list).toHaveBeenLastCalledWith('c1', expect.any(AbortSignal));
    expect(row('a')).not.toBeNull();
    expect(row('b')).not.toBeNull();
    view.el.open = false; await settle();
    await open();
    expect(list).toHaveBeenCalledTimes(3);
    expect(list).toHaveBeenLastCalledWith(undefined, expect.any(AbortSignal));
    expect(row('a')).toBeNull();
  });
});
