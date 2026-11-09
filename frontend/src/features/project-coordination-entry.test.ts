// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import source from '../runtime/sections/presence.js?raw';
import pushSource from '../runtime/sections/push.js?raw';
import { createProjectCoordinationSurface } from './project-coordination-surface';
import { createSameTaskReadJoin } from './project-coordination-read-join';
import { createCoordinatorConversationGroups } from './coordinator-conversation-group';
import type { ProjectCoordinationPorts } from './project-coordination';

import { createProjectConversationNavigation } from './project-conversation-navigation';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
const passiveReads = { coordinatorReports: async () => undefined, coordinatorBudget: async () => null, coordinatorProgress: async () => null,
  workerConversations: async () => ({ conversations: [], nextCursor: null, coverage: 'complete' }) };
// presence.js also bridges the Global Router, its owner delegation and the Watchdog entries; these coordination specs stub them.
const ownerDelegationStub = 'const createRouterOwnerDelegation = () => ({ settings: document.createElement(\'details\'), destroy() {} });\n';
const routerStub = ownerDelegationStub + 'const createGlobalRouterEntry = () => ({ refresh() {}, destroy() {}, home() { return \'\'; }, conversationId() { return \'\'; } });\n'
  + 'const createRouterWatchdogEntry = () => ({ refresh() {}, destroy() {}, location() { return \'\'; }, conversationId() { return \'\'; } });\n';

it('mounts the leader above date groups using explicit binding without promoting a chat', async () => {
  document.body.innerHTML = '<div id="convList"><div class="conv-date-group">Today</div></div><div id="presenceStrip"></div>';
  const scope: Record<string, any> = {};
  const navigate = vi.fn(), bind = vi.fn(), details = vi.fn(async () => {});
  const binding = vi.fn(async () => ({ conversationId: 'leader', availability: 'available', revision: 2 }));
  const interval = vi.spyOn(globalThis, 'setInterval').mockImplementation(() => { throw new Error('no polling'); });
  try {
    new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
      'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId', 'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + routerStub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
      scope, vi.fn(), vi.fn(), createProjectCoordinationSurface, createSameTaskReadJoin, details,
      () => ({ id: 'ordinary', projectPath: '/elsewhere' }),
      { project: { ...passiveReads, coordination: binding, board: async () => ({ active: [], recentOutcomes: [] }),
        coordinationAssignments: async () => ({ assignments: [] }), bindCoordinator: bind } },
      () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', navigate, createProjectConversationNavigation(), createCoordinatorConversationGroups,
    );
    await vi.waitFor(() => expect(document.querySelector('[data-coordinator-id="leader"]')).not.toBeNull());
    const entry = document.getElementById('projectCoordinatorEntry')!;
    expect(entry.nextElementSibling?.id).toBe('convList');
    const row = entry.querySelector<HTMLElement>('.coordination-leader')!;
    expect([...row.querySelectorAll('button')].map(button => button.getAttribute('aria-label'))).toEqual(['sidebar.copyConvId']);
    row.click();
    expect(details).not.toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith('leader');
    expect(bind).not.toHaveBeenCalled();

    expect(binding).toHaveBeenCalledWith('/project', expect.any(AbortSignal));
    expect(details).not.toHaveBeenCalled();
    expect(binding).toHaveBeenCalledWith('/elsewhere', expect.any(AbortSignal));
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
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId', 'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + routerStub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    runtime, vi.fn(), vi.fn(), createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(),
    () => ({ id: 'ordinary', projectPath: '/project' }),
    { project: { ...passiveReads, coordination: async () => ({ conversationId: '', availability: 'unbound', revision: 0 }),
      board: async () => ({ active: [], recentOutcomes: [] }), coordinationAssignments: async () => ({ assignments: [] }),
      bindCoordinator: bind, createCoordinator: create } },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', navigate, createProjectConversationNavigation(), createCoordinatorConversationGroups,
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
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + routerStub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    {}, vi.fn(), vi.fn(), createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(),
    () => ({ id: active, projectPath: '/project' }),
    { project: { ...passiveReads, coordination: async () => ({ conversationId: '', availability: 'unbound', revision: 0 }),
      board: async () => ({ active: [], recentOutcomes: [] }), coordinationAssignments: async () => ({ assignments: [] }),
      createCoordinator: create } },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', navigate, navigation, createCoordinatorConversationGroups,
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

it('revalidates the sidebar binding after push reconnect and removes recovery subscriptions on teardown', async () => {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const runtime: Record<string, any> = {}, reconnects = new Set<() => void>();
  const unsubscribe = vi.fn();
  const onReconnect = (callback: () => void) => {
    reconnects.add(callback);
    return () => { reconnects.delete(callback); unsubscribe(); };
  };
  const binding = vi.fn().mockRejectedValueOnce(new Error('temporarily offline'))
    .mockResolvedValue({ conversationId: 'leader', availability: 'available', revision: 2 });
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'pushOnReconnect', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + routerStub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    runtime, vi.fn(), vi.fn(), onReconnect, createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(), () => null,
    { project: { ...passiveReads, coordination: binding, board: async () => null,
      coordinationAssignments: async () => ({ assignments: [] }) } },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', vi.fn(), createProjectConversationNavigation(), createCoordinatorConversationGroups,
  );
  try {
    await vi.waitFor(() => expect(document.querySelector('.coordination-read-error')).not.toBeNull());
    runtime.presenceRefresh();
    expect(binding).toHaveBeenCalledTimes(1);
    reconnects.forEach(callback => callback());
    await vi.waitFor(() => expect(document.querySelector('#projectCoordinatorEntry [role="button"]')).not.toBeNull());
    expect(binding).toHaveBeenCalledTimes(2);
    expect(document.querySelector('.coordination-read-error')).toBeNull();
  } finally { window.dispatchEvent(new Event('pagehide')); }
  expect(reconnects.size).toBe(0);
  expect(unsubscribe).toHaveBeenCalledTimes(1);
});

it('bridges the main coordinator card and health checks to their typed owners and router endpoints', async () => {
  const { createGlobalRouterEntry } = await import('./global-router');
  const { createRouterWatchdogEntry } = await import('./router-watchdog');
  document.body.innerHTML = '<aside id="sidebar"><div class="sidebar-header"></div><div class="sidebar-action-row"></div></aside>'
    + '<div id="convList"></div><div id="presenceStrip"></div><div class="conv-status-strip"><div id="projectBar"></div></div>';
  const runtime: Record<string, any> = {}, open = vi.fn();
  let active: { id: string; projectPath: string } | null = null;
  let folder = '';
  const folders = [{ id: 'project-folder', projectPaths: ['/project'] }];
  const router = { projectPath: '/data/router/1', conversationId: 'coordinator-router', revision: 1,
    availability: 'available', capabilities: { assign: false, sendMessage: false, steerSubagent: true }, workerModel: '' };
  const globalRouter = vi.fn(async () => ({ ...router, conversationId: '', revision: 0, availability: 'unbound' }));
  const ensureGlobalRouter = vi.fn(async () => router);
  const watchdog = { projectPath: '/data/router/1/.tofu-watchdog', conversationId: 'coordinator-watchdog', revision: 1,
    availability: 'available', capabilities: router.capabilities,
    review: { enabled: false, schedule: '7,37 * * * *', taskId: null } };
  const routerWatchdog = vi.fn(async () => ({ ...watchdog, conversationId: '', revision: 0, availability: 'unbound' }));
  const ensureRouterWatchdog = vi.fn(async () => watchdog);
  const configureRouterWatchdogReview = vi.fn(async () => watchdog);
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId', 'loadConversation',
    '_projectConversationNavigation', 'createGlobalRouterEntry', 'createRouterWatchdogEntry', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + ownerDelegationStub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    runtime, vi.fn(), vi.fn(), createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(), () => active,
    { project: { ...passiveReads, coordination: async () => ({ conversationId: '', availability: 'unbound', revision: 0 }),
      board: async () => null, coordinationAssignments: async () => ({ assignments: [] }),
      globalRouter, ensureGlobalRouter, routerWatchdog, ensureRouterWatchdog, configureRouterWatchdogReview } },
    () => folders, () => folder, open, createProjectConversationNavigation(), createGlobalRouterEntry, createRouterWatchdogEntry,
    createCoordinatorConversationGroups,
  );
  try {
    // No pinned rows above New Chat: the main coordinator takes the leader slot of the list.
    expect(document.querySelector('#sidebar .sidebar-header')!.nextElementSibling!.className).toBe('sidebar-action-row');
    const slot = document.getElementById('globalRouterEntry')!;
    expect(slot.nextElementSibling!.id).toBe('projectCoordinatorEntry');
    expect(slot.parentElement!.className).toBe('coordination-conversation-column');
    const card = slot.querySelector<HTMLElement>('.coordination-leader.global-router-card')!;
    expect(card.querySelector('.coordination-leader-label')!.textContent).toBe('globalRouter.entry');
    expect(slot.hidden).toBe(false);
    const strip = document.getElementById('presenceStrip')!;
    expect(strip.previousElementSibling!.querySelector('.router-watchdog-header')).not.toBeNull();
    const routerHeader = strip.previousElementSibling!.previousElementSibling!.querySelector('.global-router-header')!;
    // Health checks live in the main coordinator's settings, not in a sidebar entry of their own.
    const healthChecks = routerHeader.querySelector<HTMLDetailsElement>('details.router-watchdog')!;
    expect(healthChecks).not.toBeNull();
    expect(globalRouter).toHaveBeenCalledTimes(1);
    expect(routerWatchdog).toHaveBeenCalledTimes(1);
    // Unbound reads name no Coordinator chat for the composer to steer.
    expect(runtime.isCoordinatorConversation('coordinator-router')).toBe(false);
    expect(runtime.isCoordinatorConversation('coordinator-watchdog')).toBe(false);
    expect(runtime.routerConversationIds()).toEqual([]);
    card.click();
    await vi.waitFor(() => expect(open).toHaveBeenCalledWith('coordinator-router'));
    expect(ensureGlobalRouter).toHaveBeenCalledTimes(1);
    expect(runtime.isCoordinatorConversation('coordinator-router')).toBe(true);
    healthChecks.querySelector<HTMLButtonElement>('.router-watchdog-off button')!.click();
    await vi.waitFor(() => expect(ensureRouterWatchdog).toHaveBeenCalledTimes(1));
    await vi.waitFor(() => expect(runtime.isCoordinatorConversation('coordinator-watchdog')).toBe(true));
    expect(open).not.toHaveBeenCalledWith('coordinator-watchdog');
    healthChecks.querySelector<HTMLButtonElement>('.router-watchdog-on button:last-of-type')!.click();
    expect(open).toHaveBeenCalledWith('coordinator-watchdog');
    expect(runtime.isCoordinatorConversation('')).toBe(false);
    expect(configureRouterWatchdogReview).not.toHaveBeenCalled();
    // Both chats are reached from the card and its settings, never as list rows.
    expect(runtime.routerConversationIds()).toEqual(['coordinator-router', 'coordinator-watchdog']);
    // The studio's compute-stall dialog ensures the Watchdog through the same owner.
    await expect(runtime.ensureRouterWatchdogConversation()).resolves.toBe('coordinator-watchdog');

    // The router home is no project: its chat shows no project location ('1') to edit.
    const projectBar = document.getElementById('projectBar')!;
    active = { id: 'coordinator-router', projectPath: '/data/router/1' }; runtime.presenceRefresh();
    expect(projectBar.hidden).toBe(true);
    expect(card.getAttribute('aria-current')).toBe('page');
    active = { id: 'coordinator-watchdog', projectPath: '/data/router/1/.tofu-watchdog' }; runtime.presenceRefresh();
    expect(projectBar.hidden).toBe(true);
    active = { id: 'ordinary', projectPath: '/project' }; runtime.presenceRefresh();
    expect(projectBar.hidden).toBe(false);
    expect(slot.hidden).toBe(true);
    // A project folder shows its own coordinator in that slot instead.
    active = null; folder = 'project-folder'; runtime.presenceRefresh();
    expect(slot.hidden).toBe(true);
    folder = ''; runtime.presenceRefresh();
    expect(slot.hidden).toBe(false);
  } finally { window.dispatchEvent(new Event('pagehide')); }
  expect(document.querySelector('.global-router-card')).toBeNull();
  expect(document.querySelector('.router-watchdog')).toBeNull();
});

function frozen<T>(value: T): T {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(child => frozen(child));
    Object.freeze(value);
  }
  return value;
}
// Joined callers share one result object, so every read below answers with frozen facts.
const facts = frozen({
  coordination: { projectPath: '/project', conversationId: 'leader', revision: 4, availability: 'available',
    capabilities: { assign: false, sendMessage: false, steerSubagent: false } },
  board: { active: [{ id: 'research', conversationId: 'worker', title: 'Research', status: 'active' }],
    recentOutcomes: [{ id: 'result', conversationId: 'worker', status: 'completed', resultSummary: 'Result evidence' }] },
  coordinationAssignments: { olderCursor: null, newerCursor: null, assignments: [{ assignmentId: 'assignment-a', grantRevision: 1,
    revoked: false, canonicalDelivery: false,
    request: { projectPath: '/project', coordinatorId: 'leader', recipientId: 'worker', brief: 'Build the worker task',
      authorizationSource: { conversationId: 'leader', turnId: 'human-1', projectionRevision: 1 } },
    execution: { taskId: 'task-a', turnId: 'result-a', attemptId: 'attempt-a', status: 'running', resultRevision: 1, result: {} },
    review: null, publication: null, delivery: null }] },
  coordinatorReports: { projectPath: '/project', conversationId: 'leader', bindingRevision: 4, unread: false,
    latest: { turnId: 'report-A', ordinal: 1, createdAt: 1, title: 'Delivered report-A', kind: 'delivery', content: 'Merged.' } },
  coordinatorBudget: { projectPath: '/project', request: { id: 'request', sourceConversationId: 'leader', sourceTurnId: 'human-1',
    createdAt: 1000, usage: { tokens: 1200, modelRequests: 3, toolCalls: 9, usageComplete: true, activeWorkers: 1 } } },
  workerConversations: { conversations: [{ conversationId: 'worker', title: 'Worker', turnCount: 1, coordinatorId: 'leader' }],
    nextCursor: null, coverage: 'complete' },
  coordinatorProgress: { projectPath: '/project', conversationId: 'leader', bindingRevision: 4, board: { revision: 1, updatedAt: 1,
    summary: 'Ship the release by Friday.', tasks: [{ title: 'Ship the release', status: 'in_progress' }] } },
});
const coordinatorReads = () => ({ ...Object.fromEntries(Object.entries(facts)
  .map(([name, value]) => [name, vi.fn(async () => value)])),
  workerConversations: vi.fn(async () => ({ conversations: [], nextCursor: null, coverage: 'complete' })) }) as Record<keyof typeof facts, ReturnType<typeof vi.fn>>;
const issued = (reads: Record<string, ReturnType<typeof vi.fn>>) =>
  Object.fromEntries(Object.entries(reads).map(([name, read]) => [name, read.mock.calls.length]));
const nextTask = () => new Promise(resolve => setTimeout(resolve, 0));
const projectFrame = { channel: 'project', taskId: '0123456789abcdef', type: 'project.changed' };

/** The shipped push.js over a scripted socket: each delivered frame is one message task. */
function pushTransport() {
  const sockets: Socket[] = [];
  class Socket {
    static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
    readyState = Socket.CONNECTING;
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onclose: ((event: { code: number }) => void) | null = null;
    constructor() { sockets.push(this); }
    send() {}
    close() { this.readyState = Socket.CLOSED; }
  }
  const push = new Function('runtimeScope', 'WebSocket', 'apiUrl',
    `${pushSource}\nreturn { pushSubscribe, pushUnsubscribe, pushOnReconnect };`)({}, Socket, (path: string) => path);
  const socket = () => sockets[sockets.length - 1];
  return { ...push,
    open() { socket().readyState = Socket.OPEN; socket().onopen!(); },
    deliver() { socket().onmessage!({ data: JSON.stringify(projectFrame) }); },
    close() { socket().onclose!({ code: 1000 }); } };
}

/** Canvas and sidebar both open on the bound Coordinator, wired to the real push transport. */
async function mountCoordinatorProject(reads: Record<string, unknown>) {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const push = pushTransport();
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'pushOnReconnect', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + routerStub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    {}, push.pushSubscribe, push.pushUnsubscribe, push.pushOnReconnect, createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(),
    () => ({ id: 'leader', projectPath: '/project' }), { project: reads },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', vi.fn(), createProjectConversationNavigation(), createCoordinatorConversationGroups,
  );
  push.open();
  await vi.waitFor(() => expect(document.querySelector('#presenceStrip .coordinator-progress')).not.toBeNull());
  await nextTask();
  return push;
}

/** The scope ports presence.js hands to the canvas and the sidebar surface, in that order. */
function presencePorts(project: Record<string, unknown>): ProjectCoordinationPorts[] {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const mounted: { scope(): ProjectCoordinationPorts | null }[] = [];
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + routerStub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    {}, vi.fn(), vi.fn(), (ports: { scope(): ProjectCoordinationPorts | null }) => {
      mounted.push(ports); return { refresh() {}, destroy() {} };
    }, createSameTaskReadJoin, vi.fn(), () => ({ id: 'leader', projectPath: '/project' }), { project },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', vi.fn(), createProjectConversationNavigation(), createCoordinatorConversationGroups,
  );
  return mounted.map(ports => ports.scope()!);
}

/** Reads that stay in flight until resolved and reject like fetch when their own signal aborts. */
function pendingReads() {
  const requests: { signal: AbortSignal; resolve(value: unknown): void }[] = [];
  const read = vi.fn((...args: unknown[]) => new Promise((resolve, reject) => {
    const signal = args[args.length - 1] as AbortSignal;
    requests.push({ signal, resolve });
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  }));
  return { read, requests };
}

it.each([
  ['a Project Router', '', '', true, true],
  ['the router home', '/project', '', false, true],
  ['the Watchdog', '', '/project', false, false],
])('places the Worker model control on the Coordinator conversation of %s', async (_name, home, watchdog, shown, board) => {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const reads = coordinatorReads();
  const routerWorkerModel = vi.fn(async () => ({ ...facts.coordination, workerModel: 'strong-model' }));
  const subscribe = vi.fn();
  const stub = `const createGlobalRouterEntry = () => ({ refresh() {}, destroy() {}, home() { return ${JSON.stringify(home)}; }, conversationId() { return ${JSON.stringify(home ? 'leader' : '')}; } });\n`
    + `const createRouterWatchdogEntry = () => ({ refresh() {}, destroy() {}, location() { return ${JSON.stringify(watchdog)}; }, conversationId() { return ${JSON.stringify(watchdog ? 'leader' : '')}; } });\n`
    + ownerDelegationStub;
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + stub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    {}, subscribe, vi.fn(), createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(),
    () => ({ id: 'leader', projectPath: '/project' }), { project: { ...reads, routerWorkerModel } },
    () => [{ id: 'folder', projectPaths: ['/project'] }], () => 'folder', vi.fn(), createProjectConversationNavigation(), createCoordinatorConversationGroups,
  );
  const info = vi.spyOn(console, 'info').mockImplementation(() => {});
  try {
    await vi.waitFor(() => expect(reads.coordination).toHaveBeenCalledWith('/project', expect.any(AbortSignal)));
    // The main coordinator's card stands in for the router home and the Watchdog, never a project row.
    if (shown) {
      await vi.waitFor(() => expect(document.querySelector('#projectCoordinatorEntry .coordination-leader:not([aria-busy])'))
        .not.toBeNull());
    } else expect(document.querySelector('#projectCoordinatorEntry .coordination-leader')).toBeNull();
    await vi.waitFor(() => expect(!!document.querySelector('#presenceStrip .coordinator-progress')).toBe(board));
    await nextTask();
    // The Watchdog authors no task board, so its conversation shows none and never reads one.
    expect(!!document.querySelector('#presenceStrip .coordinator-progress')).toBe(board);
    expect(reads.coordinatorProgress.mock.calls.length > 0).toBe(board);
    const control = document.querySelector<HTMLElement>('#presenceStrip .coordinator-progress-actions > .router-worker-model');
    if (!shown) {
      expect(control).toBeNull();
      expect(routerWorkerModel).not.toHaveBeenCalled();
      return;
    }
    expect(routerWorkerModel).toHaveBeenCalledWith('/project', expect.any(AbortSignal));
    expect(control!.querySelector('select')!.value).toBe('strong-model');
    // The control leads the task board's commands instead of taking a line of its own above the board.
    expect(control!.parentElement?.firstElementChild).toBe(control);
    expect(document.querySelector('#presenceStrip > .router-worker-model')).toBeNull();
    // A repaint for new project facts keeps the same attached control and its one read.
    reads.coordinatorProgress.mockResolvedValue({ ...facts.coordinatorProgress, board: {
      ...facts.coordinatorProgress.board, revision: 2, summary: 'Released; writing the notes.' } });
    subscribe.mock.calls.forEach(([, , refresh]) => refresh());
    await vi.waitFor(() => expect(document.querySelector('#presenceStrip .coordinator-progress-summary')?.textContent)
      .toBe('Released; writing the notes.'));
    await nextTask();
    expect(document.querySelector('#presenceStrip .coordinator-progress-actions > .router-worker-model')).toBe(control);
    expect(control!.isConnected).toBe(true);
    expect(routerWorkerModel).toHaveBeenCalledTimes(1);
  } finally { window.dispatchEvent(new Event('pagehide')); info.mockRestore(); }
  expect(document.querySelector('.router-worker-model')).toBeNull();
});

it.each([
  ['the Global Router', '/data/router/1', 'coordinator-router', false, true],
  ['the Watchdog', '/data/router/1/.tofu-watchdog', 'coordinator-watchdog', false, false],
  ['a Project Router', '/project', 'leader', true, true],
])('with no folder selected, %s gets a project Coordinator row only when the main coordinator card does not stand in', async (
  _name, path, id, row, board) => {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const reads = coordinatorReads();
  reads.coordination.mockResolvedValue({ ...facts.coordination, projectPath: path, conversationId: id });
  reads.coordinatorProgress.mockResolvedValue({ ...facts.coordinatorProgress, projectPath: path, conversationId: id });
  const stub = 'const createGlobalRouterEntry = () => ({ refresh() {}, destroy() {}, home() { return "/data/router/1"; }, conversationId() { return "coordinator-router"; } });\n'
    + 'const createRouterWatchdogEntry = () => ({ refresh() {}, destroy() {}, location() { return "/data/router/1/.tofu-watchdog"; }, conversationId() { return "coordinator-watchdog"; } });\n'
    + ownerDelegationStub;
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', 'const conversations = [];\n' + stub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    {}, vi.fn(), vi.fn(), createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(),
    () => ({ id, projectPath: path }), { project: reads }, () => [], () => '', vi.fn(), createProjectConversationNavigation(), createCoordinatorConversationGroups,
  );
  try {
    await vi.waitFor(() => expect(reads.coordination).toHaveBeenCalledWith(path, expect.any(AbortSignal)));
    await vi.waitFor(() => expect(!!document.querySelector('#presenceStrip .coordinator-progress')).toBe(board));
    await nextTask();
    // The main coordinator's card already stands in that slot for the router home and the
    // Watchdog; a project row would label them a project Coordinator.
    expect(document.querySelector(`#projectCoordinatorEntry [data-coordinator-id="${id}"]`) !== null).toBe(row);
    if (!row) expect(document.getElementById('projectCoordinatorEntry')!.childElementCount).toBe(0);
    expect(document.getElementById('globalRouterEntry')!.hidden).toBe(row);
  } finally { window.dispatchEvent(new Event('pagehide')); }
});

/** presence.js over a mutable folder list, with the main coordinator card and the folder settings opener. */
function mountFolderView(folders: { id: string; name: string; projectPaths: string[] }[], selected: () => string,
  reads: Record<string, unknown>, conversations: unknown[] = []) {
  document.body.innerHTML = '<div id="convList"></div><div id="presenceStrip"></div>';
  const runtime: Record<string, any> = {}, settings = vi.fn();
  const stub = 'const createGlobalRouterEntry = () => ({ refresh() {}, destroy() {}, home() { return "/data/router/1"; }, conversationId() { return "coordinator-router"; } });\n'
    + 'const createRouterWatchdogEntry = () => ({ refresh() {}, destroy() {}, location() { return ""; }, conversationId() { return ""; } });\n'
    + 'const _convStatusFlags = () => ({});\n' + ownerDelegationStub;
  new Function('runtimeScope', 'pushSubscribe', 'pushUnsubscribe', 'createProjectCoordinationSurface', 'createSameTaskReadJoin',
    'openProjectCoordinationWorkspace', 'getActiveConv', 'Api', 'getFolders', 'getActiveFolderId',
    'loadConversation', '_projectConversationNavigation', 'createCoordinatorConversationGroups', '_promptRenameFolder', 'conversations',
    stub + 'const mergeConversationCatalogRows = () => 0; const renderConversationList = () => {};\n' + source)(
    runtime, vi.fn(), vi.fn(), createProjectCoordinationSurface, createSameTaskReadJoin, vi.fn(), () => null, { project: reads },
    () => folders, selected, vi.fn(), createProjectConversationNavigation(), createCoordinatorConversationGroups, settings, conversations,
  );
  return { runtime, settings };
}

// A folder's name never decides the leader slot. One that names no directory has no project to bind
// yet, but it is still the folder's slot, not the main coordinator's.
it.each(['面试', 'Interview', '日本語の会議', '🚀 launch notes'])(
  'a selected folder without a project directory (%s) keeps the project Coordinator slot', async (name) => {
    const reads = coordinatorReads();
    reads.coordination.mockResolvedValue({ ...facts.coordination, conversationId: '', availability: 'unbound', revision: 0 });
    const folders = [{ id: 'folder', name, projectPaths: [] as string[] }];
    let selected = 'folder';
    const { runtime, settings } = mountFolderView(folders, () => selected, reads);
    const slot = document.getElementById('globalRouterEntry')!;
    try {
      const row = document.querySelector<HTMLElement>('#projectCoordinatorEntry .coordination-leader');
      expect(row).not.toBeNull();
      expect(slot.hidden).toBe(true);
      expect(row!.dataset.coordinatorId).toBe('');
      expect(row!.querySelector('.coordination-leader-label')!.textContent).toBe('projectCoordination.coordinator');
      expect(row!.querySelector('.coordination-leader-meta')!.textContent).toBe('projectCoordination.noDirectory');
      // Without a directory there is no binding to read; the row opens the folder's settings to add one.
      expect(reads.coordination).not.toHaveBeenCalled();
      const actions = [...row!.querySelectorAll('button')];
      expect(actions.map(button => button.textContent)).toEqual(['folder.settings.title']);
      actions[0].click();
      expect(settings).toHaveBeenCalledWith('folder');
      // The uncategorized view keeps the main coordinator's card in that slot.
      selected = ''; runtime.presenceRefresh();
      expect(slot.hidden).toBe(false);
      expect(document.querySelector('#projectCoordinatorEntry .coordination-leader')).toBeNull();
      // Once the folder names a directory, its row reads that exact project's binding.
      const root = `/srv/${name}`;
      folders[0].projectPaths = [root]; selected = 'folder'; runtime.presenceRefresh();
      await vi.waitFor(() => expect(document.querySelector('#projectCoordinatorEntry button')?.textContent)
        .toBe('projectCoordination.create'));
      expect(reads.coordination).toHaveBeenCalledWith(root, expect.any(AbortSignal));
      expect(slot.hidden).toBe(true);
    } finally { window.dispatchEvent(new Event('pagehide')); }
  });

it.each(['/srv/测试项目', '/srv/プロジェクト'.normalize('NFD'), '/srv/emoji 🚀 proj'])(
  'groups the Workers of a Coordinator whose project directory is %s beneath its row', async (root) => {
    const reads = coordinatorReads();
    reads.coordination.mockResolvedValue({ ...facts.coordination, projectPath: root });
    const worker = { id: 'worker', title: 'Worker', projectPath: root, folderId: 'folder', _coordinatorSource: 'leader', updatedAt: 2 };
    const { runtime } = mountFolderView([{ id: 'folder', name: '项目', projectPaths: [root] }], () => 'folder', reads, [worker]);
    try {
      await vi.waitFor(() => expect(document.querySelector('#projectCoordinatorEntry [data-coordinator-id="leader"]')).not.toBeNull());
      runtime.presenceRefresh();
      await vi.waitFor(() => expect(reads.workerConversations).toHaveBeenCalledWith(root, undefined, expect.any(AbortSignal)));
      const group = document.querySelector<HTMLElement>('#projectCoordinatorEntry + .coordinator-conversations-host .coordinator-conversations');
      await vi.waitFor(() => expect(group?.hidden).toBe(false));
      expect(group!.dataset.coordinatorId).toBe('leader');
      expect(group!.querySelector('.coordinator-conversations-count')!.textContent).toBe('1');
      expect(reads.coordination).toHaveBeenCalledWith(root, expect.any(AbortSignal));
    } finally { window.dispatchEvent(new Event('pagehide')); }
  });

it('issues each coordination read once per push frame for the canvas and sidebar together', async () => {
  const info = vi.spyOn(console, 'info').mockImplementation(() => {});
  const reads = coordinatorReads();
  const push = await mountCoordinatorProject(reads);
  try {
    Object.values(reads).forEach(read => read.mockClear());
    push.deliver();
    // push.js runs both surface handlers inside this message task. The session-free
    // canvas reads only the binding, the task board and message attention.
    const expected = { coordination: 1, board: 0, coordinationAssignments: 0, coordinatorReports: 1,
      coordinatorBudget: 0, workerConversations: 0, coordinatorProgress: 1 };
    expect(issued(reads)).toEqual(expected);
    await nextTask();
    expect(issued(reads)).toEqual(expected);
    // Both surfaces painted from the one shared, frozen result of each read.
    expect(document.querySelector('#projectCoordinatorEntry .coordination-leader-meta')?.textContent).toBe('coordinatorProgress.sidebar.active');
    expect(document.querySelector('#presenceStrip .coordinator-progress-summary')?.textContent).toBe('Ship the release by Friday.');
  } finally { window.dispatchEvent(new Event('pagehide')); push.close(); info.mockRestore(); }
});

it('issues new reads for a push frame in a later task', async () => {
  const info = vi.spyOn(console, 'info').mockImplementation(() => {});
  const reads = coordinatorReads();
  const push = await mountCoordinatorProject(reads);
  try {
    Object.values(reads).forEach(read => read.mockClear());
    push.deliver();
    await vi.waitFor(() => expect(reads.coordinatorProgress).toHaveBeenCalledOnce());
    await nextTask();
    push.deliver();
    expect(issued(reads)).toEqual({ coordination: 2, board: 0, coordinationAssignments: 0, coordinatorReports: 2,
      coordinatorBudget: 0, workerConversations: 0, coordinatorProgress: 2 });
  } finally { window.dispatchEvent(new Event('pagehide')); push.close(); info.mockRestore(); }
});

it('never joins a read still in flight from an earlier task', async () => {
  const binding = pendingReads();
  const [canvas, sidebar] = presencePorts({ coordination: binding.read });
  const earlier = canvas.binding('/project', new AbortController().signal);
  await nextTask();
  const later = sidebar.binding('/project', new AbortController().signal);
  expect(binding.read).toHaveBeenCalledTimes(2);
  binding.requests[1].resolve(facts.coordination);
  await expect(later).resolves.toBe(facts.coordination);
  binding.requests[0].resolve(null);
  await expect(earlier).resolves.toBeNull();
});

it('rejects only an aborting caller and aborts the shared read when its last caller leaves', async () => {
  const binding = pendingReads(), board = pendingReads();
  const [canvas, sidebar] = presencePorts({ coordination: binding.read, board: board.read });
  const canvasRead = new AbortController(), sidebarRead = new AbortController();
  const kept = canvas.binding('/project', canvasRead.signal);
  const left = sidebar.binding('/project', sidebarRead.signal);
  expect(binding.read).toHaveBeenCalledOnce();
  sidebarRead.abort();
  await expect(left).rejects.toBe(sidebarRead.signal.reason);
  expect(binding.requests[0].signal.aborted).toBe(false);
  binding.requests[0].resolve(facts.coordination);
  await expect(kept).resolves.toBe(facts.coordination);

  const first = new AbortController(), second = new AbortController();
  const firstBoard = canvas.board('/project', first.signal);
  const secondBoard = sidebar.board('/project', second.signal);
  first.abort();
  await expect(firstBoard).rejects.toBe(first.signal.reason);
  expect(board.requests[0].signal.aborted).toBe(false);
  second.abort();
  await expect(secondBoard).rejects.toBe(second.signal.reason);
  expect(board.requests[0].signal.aborted).toBe(true);
  expect(board.read).toHaveBeenCalledOnce();

  // A read retired by its only caller is not joined again, even in the same task.
  const retired = new AbortController();
  const gone = canvas.board('/project', retired.signal);
  retired.abort();
  const fresh = sidebar.board('/project', new AbortController().signal);
  expect(board.read).toHaveBeenCalledTimes(3);
  expect(board.requests[1].signal.aborted).toBe(true);
  await expect(gone).rejects.toBe(retired.signal.reason);
  board.requests[2].resolve(facts.board);
  await expect(fresh).resolves.toBe(facts.board);
});

it('never joins reads whose path, cursor or command differ', () => {
  const reads = coordinatorReads();
  const [canvas, sidebar] = presencePorts(reads);
  const signal = new AbortController().signal;
  void canvas.binding('/project', signal); void sidebar.binding('/other', signal);
  void canvas.dispatch!.list('/project', undefined, undefined, signal);
  void sidebar.dispatch!.list('/project', undefined, { cursor: 'older' }, signal);
  void sidebar.dispatch!.list('/project', 'command-1', undefined, signal);
  void sidebar.dispatch!.list('/project', undefined, { assignmentId: 'assignment-a' }, signal);
  void canvas.workerConversations!('/project', undefined, signal);
  void sidebar.workerConversations!('/project', 'page-2', signal);
  expect(reads.coordination).toHaveBeenCalledTimes(2);
  expect(reads.coordinationAssignments).toHaveBeenCalledTimes(4);
  expect(reads.workerConversations).toHaveBeenCalledTimes(2);
  expect(reads.coordinationAssignments).toHaveBeenCalledWith('/project', 'command-1', undefined, expect.any(AbortSignal));
  expect(reads.workerConversations).toHaveBeenCalledWith('/project', 'page-2', expect.any(AbortSignal));
  // Identical arguments within the task share the request already issued.
  void canvas.dispatch!.list('/project', undefined, { cursor: 'older' }, signal);
  void canvas.workerConversations!('/project', 'page-2', signal);
  void sidebar.binding('/project', signal);
  expect(reads.coordinationAssignments).toHaveBeenCalledTimes(4);
  expect(reads.workerConversations).toHaveBeenCalledTimes(2);
  expect(reads.coordination).toHaveBeenCalledTimes(2);
});
