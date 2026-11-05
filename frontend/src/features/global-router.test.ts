// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { createGlobalRouterEntry, type GlobalRouterPorts } from './global-router';
import type { RouterState } from '../api/router.generated';
vi.mock('../i18n', () => ({ t: (key: string, values?: unknown) => key + (values ? JSON.stringify(values) : '') }));

const capabilities = { assign: false as const, sendMessage: false as const, steerSubagent: true };
const available: RouterState = { projectPath: '/data/router/1', conversationId: 'coordinator-router', revision: 1,
  availability: 'available', capabilities, workerModel: '' };
const unbound: RouterState = { ...available, conversationId: '', revision: 0, availability: 'unbound' };

function setup(read: RouterState | Error, ensure: RouterState | Error = available) {
  let active = '';
  const ports: GlobalRouterPorts = {
    read: vi.fn(() => (read instanceof Error ? Promise.reject(read) : Promise.resolve(read))),
    ensure: vi.fn(() => (ensure instanceof Error ? Promise.reject(ensure) : Promise.resolve(ensure))),
    open: vi.fn((id: string) => { active = id; }),
    activeConversationId: () => active,
    notifyError: vi.fn(),
    changed: vi.fn(),
    workerModel: {
      read: vi.fn(async () => available),
      write: vi.fn(async (_path: string, model: string | null) => ({ ...available, workerModel: model ?? '' })),
      catalog: { models: () => [{ model_id: 'strong-model' }], visible: () => true, label: (id: string) => id },
    },
    instructions: {
      list: vi.fn(async () => ({ instructions: [], nextCursor: null })),
      pause: vi.fn(), resume: vi.fn(), cancel: vi.fn(),
    },
  };
  const sidebar = document.createElement('div'), header = document.createElement('div');
  const contributed = document.createElement('details');
  contributed.className = 'contributed-setting';
  document.body.replaceChildren(sidebar, header);
  const entry = createGlobalRouterEntry(ports, { sidebar, header, controls: [contributed] });
  const card = sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  const view = header.querySelector('header')!;
  return { ports, entry, card, view, contributed, setActive: (id: string) => { active = id; } };
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0));

describe('Global Router entry (the main coordinator)', () => {
  it('is a coordinator card that names its role, then ensures and opens the router', async () => {
    const { ports, entry, card, view } = setup(unbound);
    // Same card as a project coordinator: mark, label, one-line role, chevron.
    expect(card.getAttribute('role')).toBe('button');
    expect(card.tabIndex).toBe(0);
    expect(card.querySelector('.coordination-leader-mark svg')).not.toBeNull();
    expect(card.querySelector('.coordination-leader-label')!.textContent).toBe('globalRouter.entry');
    expect(card.querySelector('.coordination-leader-meta')!.textContent).toBe('globalRouter.entryMeta');
    expect(card.title).toBe('globalRouter.entryTitle');
    expect(card.querySelector('[data-coordinator-id]')).toBeNull();
    await settle();
    expect(view.hidden).toBe(true);
    expect(entry.conversationId()).toBe('');
    card.click();
    await settle();
    expect(ports.ensure).toHaveBeenCalledTimes(1);
    expect(ports.open).toHaveBeenCalledWith('coordinator-router');
    expect(entry.conversationId()).toBe('coordinator-router');
    expect(view.hidden).toBe(false);
    expect(card.getAttribute('aria-current')).toBe('page');
    // The topbar already names the chat: the header explains it instead of repeating the name.
    expect(view.textContent).not.toContain('globalRouter.entry');
    expect(view.querySelector('.global-router-header-subtitle')!.textContent).toBe('globalRouter.headerSubtitle');
    expect(view.textContent).toContain('globalRouter.workerModelDefault');
  });

  it('opens from the keyboard like the project card', async () => {
    const { ports, card } = setup(available);
    await settle();
    card.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await settle();
    expect(ports.open).toHaveBeenCalledWith('coordinator-router');
    card.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
    await settle();
    expect(ports.ensure).toHaveBeenCalledTimes(1);
  });

  it('identifies only the router conversation from the passive read', async () => {
    const { ports, entry, view, card, setActive } = setup({ ...available, workerModel: 'strong-model' });
    await settle();
    expect(ports.ensure).not.toHaveBeenCalled();
    expect(entry.conversationId()).toBe('coordinator-router');
    setActive('some-other-chat'); entry.refresh();
    expect(view.hidden).toBe(true);
    expect(card.hasAttribute('aria-current')).toBe(false);
    setActive('coordinator-router'); entry.refresh();
    expect(view.hidden).toBe(false);
    expect(view.textContent).toContain('strong-model');
  });

  it('reports failures explicitly and ignores repeated activation while ensuring', async () => {
    const { ports, card } = setup(new Error('offline'), new Error('home not writable'));
    card.click(); card.click();
    expect(card.getAttribute('aria-busy')).toBe('true');
    await settle();
    expect(ports.ensure).toHaveBeenCalledTimes(1);
    expect(ports.open).not.toHaveBeenCalled();
    expect(ports.notifyError).toHaveBeenCalledWith(expect.stringContaining('home not writable'));
    expect(card.hasAttribute('aria-busy')).toBe(false);
    expect(card.hasAttribute('aria-disabled')).toBe(false);
  });

  it('hosts its Worker model and standing instructions before the settings other owners contribute', async () => {
    const { ports, entry, view } = setup({ ...available, workerModel: 'strong-model' });
    await settle();
    expect(entry.home()).toBe('/data/router/1');
    expect(ports.changed).toHaveBeenCalledTimes(1);
    // The state already carries the Worker model; the control does not read it again.
    expect(ports.workerModel.read).not.toHaveBeenCalled();
    const select = view.querySelector<HTMLSelectElement>('.router-worker-model select')!;
    expect(select.value).toBe('strong-model');
    select.value = ''; select.dispatchEvent(new Event('change'));
    await settle();
    expect(ports.workerModel.write).toHaveBeenCalledWith('/data/router/1', null);
    expect(ports.instructions.list).not.toHaveBeenCalled();
    expect([...view.children].map(child => child.className)).toEqual([
      'global-router-header-subtitle', 'router-worker-model', 'router-instructions', 'contributed-setting']);
  });

  it('knows the router home from an unbound read without offering router controls', async () => {
    const { ports, entry, view, contributed } = setup(unbound);
    await settle();
    expect(entry.home()).toBe('/data/router/1');
    expect(ports.changed).toHaveBeenCalledTimes(1);
    expect(view.querySelector('.router-worker-model, .router-instructions')).toBeNull();
    expect(contributed.parentElement).toBe(view);
  });

  it('announces a new router conversation, not only a new home, so lists can stop showing it', async () => {
    const { ports, card } = setup(unbound);
    await settle();
    expect(ports.changed).toHaveBeenCalledTimes(1);
    card.click();
    await settle();
    expect(ports.changed).toHaveBeenCalledTimes(2);
    card.click();
    await settle();
    expect(ports.changed).toHaveBeenCalledTimes(2);
  });

  it('removes its DOM and stops reacting after destroy', async () => {
    const { entry, card, view, contributed, setActive } = setup(available);
    await settle();
    entry.destroy();
    setActive('coordinator-router'); entry.refresh();
    expect(card.isConnected).toBe(false);
    expect(view.isConnected).toBe(false);
    expect(contributed.isConnected).toBe(false);
  });
});
