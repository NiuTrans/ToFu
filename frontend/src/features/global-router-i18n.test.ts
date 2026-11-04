// @vitest-environment jsdom
// The main coordinator's card is created by the retained presence section
// before i18n ready(): its labels must follow the catalog once it arrives and
// after every language switch, never staying the raw key ('globalRouter.entry').
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../i18n/locales/en.json';
import zh from '../i18n/locales/zh.json';
import type { RouterState } from '../api/router.generated';

const router: RouterState = { projectPath: '/data/router/1', conversationId: 'coordinator-router', revision: 1,
  availability: 'available', capabilities: { assign: false, sendMessage: false, steerSubagent: true }, workerModel: '' };

beforeEach(() => { vi.resetModules(); localStorage.clear(); document.body.replaceChildren(); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); document.body.replaceChildren(); });

it('translates the card created before the locale catalog loads, then follows a language switch', async () => {
  let releaseZh!: () => void;
  const zhArrived = new Promise<void>(resolve => { releaseZh = resolve; });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const english = String(url).includes('/en.generated');
    if (!english) await zhArrived;
    return { ok: true, status: 200, json: async () => (english ? en : zh) };
  }));
  const i18n = await import('../i18n');
  const { createGlobalRouterEntry } = await import('./global-router');
  const sidebar = document.createElement('div'), header = document.createElement('div');
  document.body.append(sidebar, header);
  const entry = createGlobalRouterEntry({
    read: async () => router, ensure: async () => router, open() {}, notifyError() {},
    activeConversationId: () => router.conversationId,
    workerModel: { read: async () => router, write: async () => router,
      catalog: { models: () => [], visible: () => true, label: (id: string) => id } },
    instructions: { list: async () => ({ instructions: [], nextCursor: null }),
      pause: async () => { throw new Error('unused'); }, resume: async () => { throw new Error('unused'); },
      cancel: async () => { throw new Error('unused'); } },
  }, { sidebar, header });
  const card = sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  const label = () => card.querySelector('.coordination-leader-label')!.textContent;
  const meta = () => card.querySelector('.coordination-leader-meta')!.textContent;
  const workerLabel = () => header.querySelector('.router-worker-model label')!.textContent;
  const workerHelp = () => header.querySelector<HTMLSelectElement>('.router-worker-model select')!.title;
  await vi.waitFor(() => expect(header.querySelector('.router-worker-model')).not.toBeNull());

  // Painted before the catalog arrived: lookups are premature and return keys.
  expect(label()).toBe('globalRouter.entry');
  expect(workerLabel()).toBe('routerWorkerModel.label');
  expect(workerHelp()).toBe('routerWorkerModel.help');

  const ready = i18n.ready();
  releaseZh();
  await ready;
  expect(label()).toBe('总负责人');
  expect(meta()).toBe(zh['globalRouter.entryMeta']);
  expect(card.title).toBe(zh['globalRouter.entryTitle']);
  expect(header.querySelector('.global-router-header-subtitle')!.textContent).toBe(zh['globalRouter.headerSubtitle']);
  expect(workerLabel()).toBe(zh['routerWorkerModel.label']);
  expect(workerHelp()).toBe(zh['routerWorkerModel.help']);
  expect(header.querySelector('.router-worker-model option')!.textContent).toBe(zh['routerWorkerModel.ownerDefault']);
  expect(header.querySelector('.router-instructions summary')!.textContent).toBe(zh['routerInstructions.title']);

  await i18n.setLanguage('en');
  expect(label()).toBe('Main coordinator');
  expect(meta()).toBe(en['globalRouter.entryMeta']);
  expect(card.title).toBe(en['globalRouter.entryTitle']);
  expect(workerLabel()).toBe(en['routerWorkerModel.label']);
  expect(workerHelp()).toBe(en['routerWorkerModel.help']);
  expect(header.querySelector('.router-worker-model option')!.textContent).toBe('Your default model');
  expect(header.querySelector('.router-instructions summary')!.textContent).toBe('Standing instructions');
  entry.destroy();
});
