// @vitest-environment jsdom
/** Execute retained lifecycle/send adapters; the typed controller remains the sole draft owner. */
import { describe, expect, it, vi } from 'vitest';
import { captureConversationProject, createConversationProjectController, withProjectHydration } from './conversation-project';
import { buildConversationSettingsSnapshot, createConversationSettingsResolution } from './conversation-settings-resolution';
import { retryTransientSendStartup } from '../../core/send-startup';
import { createProjectConversationNavigation } from '../../features/project-conversation-navigation';
import { createProjectCoordinationSurface } from '../../features/project-coordination-surface';
import { createSameTaskReadJoin } from '../../features/project-coordination-read-join';
import { createCoordinatorConversationGroups } from '../../features/coordinator-conversation-group';
import { createGlobalRouterEntry } from '../../features/global-router';
import { createRouterWatchdogEntry } from '../../features/router-watchdog';
import type { RouterState, RouterWatchdogState } from '../../api/router.generated';

import { paintNewChatFolderBadges } from '../ui/new-chat-folder-badges';
const { readFileSync } = await vi.importActual<{
  readFileSync(path: string, encoding: 'utf8'): string;
}>('node:fs');
const vm = await vi.importActual<{
  createContext(values: Record<string, unknown>): unknown;
  runInContext(source: string, context: unknown): unknown;
}>('node:vm');
const { cwd } = await vi.importActual<{ cwd(): string }>('node:process');
const retained = ['main_conv_lifecycle.js', 'main_send_pipeline.js'].map(name =>
  readFileSync(`${cwd()}/frontend/src/runtime/sections/main/${name}`, 'utf8')).join('\n');
const presence = readFileSync(`${cwd()}/frontend/src/runtime/sections/presence.js`, 'utf8');
const empty = { projectPath: '', projectPaths: [], readOnlyPaths: [] };
const defaults = () => ({ projectPath: '/a', projectPaths: ['/a', '/a/lib'], readOnlyPaths: ['/a/lib'] });

function harness(input = 'work', folder: { name: string; color: unknown } | null = { name: 'folder-a', color: '' }) {
  document.body.innerHTML = '<textarea id="userInput"></textarea><div id="topbarTitle"></div><div id="chatInner"></div>';
  (document.getElementById('userInput') as HTMLTextAreaElement).value = input;
  let selectedFolder = 'folder-a';
  const folderDefaults = defaults();
  const submissions: Array<Record<string, any>> = [];
  const noop = () => {};
  const env: Record<string, any> = {
    document, sessionStorage, console, AbortController,
    activeConvId: null, conversations: [], serverModel: 'model', config: {},

    generateId: () => 'new-conversation',
    searchMode: 'multi', fetchEnabled: false, codeExecEnabled: false, browserEnabled: false,
    desktopEnabled: false, memoryEnabled: false, schedulerEnabled: false, autopilotEnabled: false,
    imageGenEnabled: false, imageGenMode: false, humanGuidanceEnabled: false,
    autoTranslate: false, activeFlow: '', autoApplyWrites: true, isGenerating: false,
    pendingImages: [], pendingPdfTexts: [], pendingVideos: [],

    _pendingLogClean: null,
    captureComposerModel: () => ({ model: 'model' }),

    commitComposerModel: noop,
    captureComposerDraft: (id: string) => ({
      convId: id, commandId: 'command', model: 'model', titleCandidate: 'title',
      inputValue: (document.getElementById('userInput') as HTMLTextAreaElement).value,
      images: [], pdfTexts: [], videos: [], replyQuotes: [], convRefs: [],
      translationModel: null, translationDefaults: null,
    }),
    testPrepare: async () => {},
    _igSelectedModel: '', _igSelectedProviderId: '', _igSelectedCount: 1,
    _igSelectedAspect: '', _igSelectedResolution: '',
    _conversationModelController: { begin: noop },
    _hasProjectComposerDraft: () => Boolean((document.getElementById('userInput') as HTMLTextAreaElement).value.trim()),
    getActiveConv: () => env.conversations.find((conv: any) => conv.id === env.activeConvId) || null,
    getActiveFolderId: vi.fn(() => selectedFolder),
    getFolderById: (id: string) => folder ? { id, ...folder } : null,
    paintNewChatFolderBadges,
    folderDefaultWorkspace: vi.fn(() => folderDefaults),
    createComposerSubmissionGate: () => ({}),
    captureConversationProject, withProjectHydration, buildConversationSettingsSnapshot,
    createProjectConversationNavigation,

    _getConvProjectPath: (conv: any) => conv.projectPath || '',
    sendFailureToastDetail: (error: Error) => error.message,
    composerTranslationStatus: { refresh: noop },
    _purgeEmptyConvs: noop, renderComposerModelNotice: noop, renderConversationList: noop,
    _conversationDisplayTitle: (value: string) => value, t: (key: string) => key, escapeHtml: (value: string) => value,
    brandLogoImgAttrs: () => '', _welcomePillsHtml: () => '', buildTurnNav: noop,
    renderPendingQueueUI: noop, updateSendButton: noop, _resetToolsToDefaults: noop,
    reconcileConversationCatalogMetadata: noop, _setComposerTaskUiHint: noop,
    captureActiveConversationSettings: noop, _showOptimisticUserTurn: () => ['user', 'assistant'], _removeOptimisticUserTurn: noop,
    _removeTranslatingBubble: noop, debugLog: vi.fn(),
    awaitSendPreparation: (promise: Promise<unknown>) => promise,
    retryTransientSendStartup,
    createSendStartupLease: () => ({ signal: new AbortController().signal, stoppedByUser: () => false, finish: noop }),
    buildTurnSubmissionExtra: (extra: unknown) => extra,
    runtimeScope: {

      requestAuthoritativeConversationRender: noop,
      ConversationTurnRead: { activeMainAttemptId: () => null },
      ConversationTurnStore: {
        hydrateConversation: async () => {},
        submitConversation: async (conv: any, msgPayload: any, sendConfig: any, extra: any) => {
          submissions.push({ conv: { ...conv }, msgPayload, sendConfig, extra });
          return { queued: true, position: 1 };
        },
      },
    },
  };
  env._conversationProjectController = createConversationProjectController({
    activeId: () => env.activeConvId,
    exists: id => env.conversations.some((conv: any) => conv.id === id),
    apply: state => { env.projectState = state; },
  });
  env._conversationSettingsResolution = createConversationSettingsResolution({
    // Simulate a server with unrelated ambient defaults; captured bindings win.
    resolveConfig: async () => ({ ...empty, folderId: 'ambient', settings: { ...empty, folderId: 'ambient' } }),
    resolveSettings: async () => ({ ...empty, folderId: 'ambient' }),
    patchResolvedSettings: async () => ({ ok: true, status: 200 }),
    patchSettings: async () => ({ ok: true, status: 200 }),
  });
  const context = vm.createContext(env);
  vm.runInContext(retained, context);
  // Presentation/consumption ports aren't the workspace authority under test.
  vm.runInContext(`
    _clearCapturedComposerDraft = () => {};
    _consumeAcceptedComposerDraft = () => {};
    _applyAcceptedConversationTitle = () => {};

    _updateOptimisticAssistantPhase = () => true;

    _waitForVlmParsing = (...args) => testPrepare(...args);
    _buildToolbarOverrides = () => ({});
  `, context);
  return {
    env, context, submissions, folderDefaults,
    selectFolder: (id: string) => { selectedFolder = id; },
    newChat: () => vm.runInContext('newChat()', context),
    send: async () => {
      const result = await vm.runInContext('_submitComposerDraft()', context);
      if (!result) throw new Error(JSON.stringify(env.debugLog.mock.calls));
      return result;
    },
  };
}

describe('retained draft folder send wiring', () => {
  it('clear -> first send does not reattach default roots', async () => {
    const h = harness(); h.newChat();
    h.env._conversationProjectController.chooseDraft(empty);
    expect(await h.send()).toBe(true);
    expect(h.submissions).toHaveLength(1);
    const sent = h.submissions[0];
    expect(sent.conv).toMatchObject({ ...empty, folderId: 'folder-a' });
    expect(sent.msgPayload.folderId).toBe('folder-a');
    expect(sent.sendConfig).toMatchObject({ ...empty, folderId: 'folder-a' });
    expect(sent.extra.settings).toMatchObject({ ...empty, folderId: 'folder-a' });
  });
  it('nonempty text gets A defaults once; switching filter to B and editing defaults cannot alter send', async () => {
    const h = harness('unsent work'); h.newChat();
    expect(h.env.projectState.path).toBe('/a');
    expect((document.getElementById('userInput') as HTMLTextAreaElement).value).toBe('unsent work');
    h.selectFolder('folder-b');
    h.folderDefaults.projectPath = '/edited'; h.folderDefaults.projectPaths.push('/edited');
    h.folderDefaults.readOnlyPaths.length = 0;
    expect(await h.send()).toBe(true);
    const sent = h.submissions[0];
    const binding = { folderId: 'folder-a', ...defaults() };
    expect(sent.conv).toMatchObject(binding);
    expect(sent.msgPayload.folderId).toBe('folder-a');
    expect(sent.sendConfig).toMatchObject(binding);
    expect(sent.extra.settings).toMatchObject(binding);
    expect(h.env.getActiveFolderId).toHaveBeenCalledTimes(1);
    expect(h.env.folderDefaultWorkspace).toHaveBeenCalledTimes(1);
  });
  it.each([empty, { projectPath: '/explicit', projectPaths: ['/explicit'], readOnlyPaths: ['/explicit'] }])(
    'newChat carries explicit private/roots target and unsent work rather than resetting to B', async choice => {
      const h = harness('unsent work'); h.newChat();
      h.env._conversationProjectController.chooseDraft(choice);
      h.selectFolder('folder-b'); h.newChat();
      expect(document.getElementById('topbarTitle')?.textContent).toContain('folder-a');
      expect(await h.send()).toBe(true);
      expect(h.submissions[0].msgPayload.folderId).toBe('folder-a');
      expect(h.submissions[0].sendConfig).toMatchObject({ folderId: 'folder-a', ...choice });
      expect(h.submissions[0].extra.settings).toMatchObject({ folderId: 'folder-a', ...choice });
    },
  );
  it('accepted command keeps folder/roots/read-only snapshot when the saved conversation changes during parsing', async () => {
    const h = harness(); h.newChat();
    let release!: () => void;
    h.env.testPrepare = () => new Promise<void>(resolve => { release = resolve; });
    const sending = h.send();
    expect(release).toBeTypeOf('function');
    Object.assign(h.env.conversations[0], { folderId: 'folder-b', projectPath: '/later', projectPaths: ['/later'], readOnlyPaths: [] });
    release();
    expect(await sending).toBe(true);
    expect(h.submissions[0].msgPayload.folderId).toBe('folder-a');
    expect(h.submissions[0].sendConfig).toMatchObject({ folderId: 'folder-a', ...defaults() });
    expect(h.submissions[0].extra.settings).toMatchObject({ folderId: 'folder-a', ...defaults() });
  });
  it('existing conversation sends its saved binding rather than the active filter', async () => {
    const h = harness(); h.newChat(); await h.send();
    h.selectFolder('folder-b'); h.folderDefaults.projectPath = '/edited';
    expect(await h.send()).toBe(true);
    expect(h.submissions[1].msgPayload.folderId).toBe('folder-a');
    expect(h.submissions[1].sendConfig).toMatchObject({ folderId: 'folder-a', ...defaults() });
    expect(h.env.folderDefaultWorkspace).toHaveBeenCalledTimes(1);
  });
});

describe('composer delivery into a live reply', () => {
  const capabilities = { assign: false as const, sendMessage: false as const, steerSubagent: true };
  const router: RouterState = { projectPath: '/data/router/1', conversationId: 'coordinator-router', revision: 1,
    availability: 'available', capabilities, workerModel: '' };
  const watchdog: RouterWatchdogState = { projectPath: '/data/router/1/.tofu-watchdog', conversationId: 'coordinator-watchdog',
    revision: 1, availability: 'available', capabilities, review: { enabled: false, schedule: '7,37 * * * *', taskId: null } };
  const folders = [{ id: 'folder-a', projectPaths: ['/project'] }, { id: 'folder-b', projectPaths: ['/other'] }];
  // The bound Coordinator of each path, as the coordination binding read reports it.
  const bindings: Record<string, string> = {
    '/project': 'coordinator-project', '/other': 'coordinator-other',
    [router.projectPath]: router.conversationId, [watchdog.projectPath]: watchdog.conversationId,
  };
  const visible = (selector: string) => expect(document.querySelector<HTMLElement>(selector)?.hidden).toBe(false);
  const chat = (id: string, projectPath: string, extra: Record<string, unknown> = {}) => ({
    id, title: id, projectPath, projectPaths: [projectPath], readOnlyPaths: [], ...extra,
  });

  /** The shipped presence bridge over the real router, Watchdog and coordination owners; every chat is replying. */
  async function liveChats() {
    const h = harness('while you reply');
    document.body.insertAdjacentHTML('beforeend', '<aside id="sidebar"><div class="sidebar-header"></div></aside>'
      + '<div id="convList"></div><div id="presenceStrip"></div>');
    const showChoice = vi.fn(async () => 'queue');
    h.env.conversations.push(
      chat(router.conversationId, router.projectPath),
      chat(watchdog.conversationId, watchdog.projectPath),
      chat('coordinator-project', '/project', { folderId: 'folder-a' }),
      chat('worker-project', '/project', { folderId: 'folder-a', _coordinatorSource: 'coordinator-project' }),
      chat('worker-router', router.projectPath, { _coordinatorSource: router.conversationId }),
      chat('ordinary', '/project', { folderId: 'folder-a' }),
    );
    h.env.runtimeScope.ConversationTurnRead = { activeMainAttemptId: (conv: unknown) => (conv ? 'live-attempt' : null) };
    h.env.composerTranslationStatus = { refresh: () => {}, begin: () => ({}), update: () => {}, finish: () => {} };
    Object.assign(h.env, {
      window, showChoice, pushSubscribe: vi.fn(), pushUnsubscribe: vi.fn(),
      createProjectCoordinationSurface, createSameTaskReadJoin, createGlobalRouterEntry, createRouterWatchdogEntry, createCoordinatorConversationGroups,
      openProjectCoordinationWorkspace: vi.fn(), getFolders: () => folders,
      // Retained conversation-list owners the Coordinator groups paint rows and status with.
      _convStatusFlags: () => ({}), mergeConversationCatalogRows: vi.fn(),
      _buildConvItemHTML: (c: { id: string; title: string }) =>
        `<div class="conv-item" data-conv-id="${c.id}"><div class="conv-title">${c.title}</div><div class="conv-date"></div></div>`,
      Api: { project: {
        workerConversations: async () => ({ conversations: [], nextCursor: null, coverage: 'complete' }),
        coordination: async (path: string) => (bindings[path]
          ? { conversationId: bindings[path], availability: 'available', revision: 1 }
          : { conversationId: '', availability: 'unbound', revision: 0 }),
        coordinatorProgress: async () => null, coordinatorReports: async () => undefined,
        globalRouter: async () => router, routerWatchdog: async () => watchdog,
      } },
    });
    vm.runInContext(presence, h.context);
    await vi.waitFor(() => {
      expect(document.querySelector('#projectCoordinatorEntry [data-coordinator-id="coordinator-project"]')).not.toBeNull();
      // Both passive reads landed: these header controls exist only for an available binding.
      expect(document.querySelector('.global-router-header .router-worker-model')).not.toBeNull();
      expect(document.querySelector<HTMLInputElement>('.router-watchdog-schedule input')?.value).toBe(watchdog.review.schedule);
    });
    return {
      ...h, showChoice,
      open(id: string) { h.env.activeConvId = id; h.env.runtimeScope.presenceRefresh(); },
      /** A folder tab only filters the sidebar: the open chat stays. */
      browse(folderId: string) { h.selectFolder(folderId); h.env.runtimeScope.presenceRefresh(); },
      close() { window.dispatchEvent(new Event('pagehide')); },
    };
  }

  /* Each chat's marker is painted from the same server binding that decides
   * delivery: the router and Watchdog headers, the Coordinator's task board. */
  it.each([
    ['Global Router', 'a project folder', router.conversationId, '.global-router-header', 'folder-a'],
    ['Watchdog', 'a project folder', watchdog.conversationId, '.router-watchdog-header', 'folder-a'],
    ['Project Coordinator', 'its own folder', 'coordinator-project', '#presenceStrip .coordinator-progress', 'folder-a'],
    ['Project Coordinator', "another project's folder", 'coordinator-project', '#presenceStrip .coordinator-progress', 'folder-b'],
  ])('the %s chat steers without the steer/queue prompt while browsing %s', async (_kind, _browsing, id, marker, folder) => {
    const chats = await liveChats();
    try {
      chats.open(id);
      chats.browse(folder);
      const shown = bindings[folders.find(item => item.id === folder)!.projectPaths[0]];
      await vi.waitFor(() => {
        visible(marker);
        visible(`#projectCoordinatorEntry [data-coordinator-id="${shown}"]`);
      });
      expect(await chats.send()).toBe(true);
      expect(chats.showChoice).not.toHaveBeenCalled();
      expect(chats.submissions).toHaveLength(1);
      expect(chats.submissions[0].conv.id).toBe(id);
      expect(chats.submissions[0].extra.injectMode).toBe('steer');
    } finally { chats.close(); }
  });

  // A Worker's return button shows its project binding was read: the chat is still no Coordinator.
  it.each([
    ['Project Worker', 'worker-project', '#presenceStrip .ui-btn'],
    ['Global Router Worker', 'worker-router', '#presenceStrip .ui-btn'],
    ['ordinary', 'ordinary', '#projectCoordinatorEntry [data-coordinator-id="coordinator-project"]'],
  ])('the %s chat still asks steer or queue', async (_kind, id, marker) => {
    const chats = await liveChats();
    try {
      chats.open(id);
      await vi.waitFor(() => visible(marker));
      expect(await chats.send()).toBe(true);
      expect(chats.showChoice).toHaveBeenCalledOnce();
      expect(chats.submissions).toHaveLength(1);
      expect(chats.submissions[0].conv.id).toBe(id);
      expect(chats.submissions[0].extra.injectMode).toBe('queue');
    } finally { chats.close(); }
  });
});

describe('actual newChat folder badge security', () => {
  it.each(['"><svg/onload=alert(1)>', '#abc\n', 'red', 'var(--untrusted)', '#abc;background:red', null, 123])(
    'renders unsafe historical color %j as automatic without any injected nodes or handlers', color => {
      const folder = { name: 'Historical folder', color };
      const h = harness('', folder);
      const alert = vi.spyOn(window, 'alert').mockImplementation(() => {});
      try {
        h.newChat();
        const topbar = document.querySelector<HTMLElement>('.topbar-folder-badge')!;
        const welcome = document.querySelector<HTMLElement>('.welcome-folder-badge')!;
        const dot = welcome.querySelector<HTMLElement>('.welcome-folder-dot')!;
        expect(topbar.textContent).toBe(`● ${folder.name}`);
        expect(welcome.textContent).toBe(`● ${folder.name}`);
        expect(topbar.style.color).toBe('var(--text-tertiary)');
        expect(dot.style.color).toBe('var(--text-tertiary)');
        expect(document.querySelector('svg, [onload], [onerror]')).toBeNull();
        // Trigger any event-bearing nodes rather than relying on jsdom's automatic events.
        document.querySelectorAll('*').forEach(node => node.dispatchEvent(new Event('load')));
        expect(alert).not.toHaveBeenCalled();
        expect(folder.color).toBe(color);
        // Repeat the actual lifecycle: neither slot accumulates old badges or injected nodes.
        h.newChat();
        expect(document.querySelectorAll('.topbar-folder-badge')).toHaveLength(1);
        expect(document.querySelectorAll('.welcome-folder-badge')).toHaveLength(1);
      } finally {
        alert.mockRestore();
      }
    },
  );
  it.each(['#abc', '#ABC', '#aBcDeF', '#6e56cf', '#3b82f6', '#10b981', '#f59e0b',
    '#ef4444', '#ec4899', '#8b5cf6', '#14b8a6'])('keeps palette color %s on both DOM badges', color => {
    const h = harness('', { name: 'Palette', color }); h.newChat();
    const expected = document.createElement('span'); expected.style.color = color;
    expect(document.querySelector<HTMLElement>('.topbar-folder-badge')!.style.color).toBe(expected.style.color);
    expect(document.querySelector<HTMLElement>('.welcome-folder-dot')!.style.color).toBe(expected.style.color);
  });
  it('renders folder name as literal text in both badges', () => {
    const name = '<svg onload=alert(1)> & " <img src=x onerror=alert(1)>';
    const h = harness('', { name, color: '#abc' }); h.newChat();
    expect(document.querySelector('.topbar-folder-badge')?.textContent).toBe(`● ${name}`);
    expect(document.querySelector('.welcome-folder-badge')?.textContent).toBe(`● ${name}`);
    expect(document.querySelector('svg, [onload], [onerror]')).toBeNull();
  });
  it('renders no folder badge without a selected folder', () => {
    const h = harness('', null); h.newChat();
    expect(document.getElementById('topbarTitle')?.textContent).toBe('New Chat');
    expect(document.querySelector('.topbar-folder-badge, .welcome-folder-badge')).toBeNull();
    expect(document.querySelector('#welcome .feature-pills')).not.toBeNull();
  });
});
