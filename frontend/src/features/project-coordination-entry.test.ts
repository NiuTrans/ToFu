// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import source from '../runtime/sections/presence.js?raw';
import { createProjectCoordinationSurface } from './project-coordination-surface';

import { createProjectConversationNavigation } from './project-conversation-navigation';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
const passiveReads = { coordinatorReports: async () => undefined, coordinatorBudget: async () => null };

it('mounts the leader above date groups using explicit binding without promoting a chat', async () => {
  document.body.innerHTML = '<div id="convList"><div class="conv-date-group">Today</div></div><div id="presenceStrip"></div>';
  const scope: Record<string, any> = {};
  const navigate = vi.fn(), bind = vi.fn(), details = vi.fn(async () => {});
  const binding = vi.fn(async () => ({ conversationId: 'leader', availability: 'available', revision: 2 }));
  const interval = vi.spyOn(globalThis, 'setInterval').mockImplementation(() => { throw new Error('no polling'); });
  try {
    new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface',
      'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId', 'loadConversation', '_projectConversationNavigation', source)(
      scope, vi.fn(), vi.fn(), createProjectCoordinationSurface, details,
      () => ({ id: 'ordinary', projectPath: '/elsewhere' }),
      { project: { ...passiveReads, coordination: binding, board: async () => ({ active: [], recentOutcomes: [] }),
        coordinationAssignments: async () => ({ assignments: [] }), bindCoordinator: bind } },
      () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', navigate, createProjectConversationNavigation(),
    );
    await vi.waitFor(() => expect(document.querySelector('[data-coordinator-id="leader"]')).not.toBeNull());
    const entry = document.getElementById('projectCoordinatorEntry')!;
    expect(entry.nextElementSibling?.id).toBe('convList');
    const row = entry.querySelector<HTMLElement>('.coordination-leader')!;
    expect(row.querySelector('button')).toBeNull();
    row.click();
    expect(details).not.toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith('leader');
    expect(bind).not.toHaveBeenCalled();

    expect(binding).toHaveBeenCalledWith('/project');
    expect(details).not.toHaveBeenCalled();
    expect(binding).toHaveBeenCalledWith('/elsewhere');
    const reads = binding.mock.calls.length;
    scope.presenceRefresh();
    expect(binding).toHaveBeenCalledTimes(reads);
    window.dispatchEvent(new Event('pagehide'));
  } finally { interval.mockRestore(); }
});

it('wires explicit atomic creation to the selected folder, never newChat or bindCurrent', async () => {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const runtime: Record<string, any> = {}, navigate = vi.fn(), bind = vi.fn();
  const create = vi.fn(async () => ({ conversationId: 'new-coordinator', availability: 'available' }));
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId', 'loadConversation', '_projectConversationNavigation', source)(
    runtime, vi.fn(), vi.fn(), createProjectCoordinationSurface, vi.fn(),
    () => ({ id: 'ordinary', projectPath: '/project' }),
    { project: { ...passiveReads, coordination: async () => ({ conversationId: '', availability: 'unbound', revision: 0 }),
      board: async () => ({ active: [], recentOutcomes: [] }), coordinationAssignments: async () => ({ assignments: [] }),
      bindCoordinator: bind, createCoordinator: create } },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', navigate, createProjectConversationNavigation(),
  );
  await vi.waitFor(() => expect(document.querySelector('#projectCoordinatorEntry button')?.textContent).toBe('projectCoordination.create'));
  expect(create).not.toHaveBeenCalled();
  (document.querySelector('#projectCoordinatorEntry button') as HTMLButtonElement).click();
  await vi.waitFor(() => expect(navigate).toHaveBeenCalledWith('new-coordinator'));
  expect(create).toHaveBeenCalledWith('/project', 'folder', 0);
  expect(bind).not.toHaveBeenCalled();
  expect(document.getElementById('presenceStrip')?.textContent).toBe('');
  window.dispatchEvent(new Event('pagehide'));
});

it.each(['conversation', 'draft', 'away-and-back'])('does not steal navigation after pending creation: %s', async (destination) => {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const navigation = createProjectConversationNavigation();
  const navigate = vi.fn();
  let active = 'ordinary';
  let finish!: (value: unknown) => void;
  const create = vi.fn(() => new Promise(resolve => { finish = resolve; }));
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', source)(
    {}, vi.fn(), vi.fn(), createProjectCoordinationSurface, vi.fn(),
    () => ({ id: active, projectPath: '/project' }),
    { project: { ...passiveReads, coordination: async () => ({ conversationId: '', availability: 'unbound', revision: 0 }),
      board: async () => ({ active: [], recentOutcomes: [] }), coordinationAssignments: async () => ({ assignments: [] }),
      createCoordinator: create } },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', navigate, navigation,
  );
  await vi.waitFor(() => expect(document.querySelector('#projectCoordinatorEntry button')).not.toBeNull());
  (document.querySelector('#projectCoordinatorEntry button') as HTMLButtonElement).click();
  expect(create).toHaveBeenCalledOnce();
  navigation.invalidate();
  active = destination === 'draft' ? '' : 'other';
  if (destination === 'away-and-back') { navigation.invalidate(); active = 'ordinary'; }
  finish({ conversationId: 'new-coordinator', availability: 'available' });
  await vi.waitFor(() => expect((document.querySelector('#projectCoordinatorEntry button') as HTMLButtonElement).disabled).toBe(false));
  expect(navigate).not.toHaveBeenCalled();
  window.dispatchEvent(new Event('pagehide'));
});
