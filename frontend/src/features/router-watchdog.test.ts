// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RouterWatchdogPorts } from './router-watchdog';
import type { RouterWatchdogState } from '../api/router.generated';

const capabilities = { assign: false as const, sendMessage: false as const, steerSubagent: true };
const review = { enabled: false, schedule: '7,37 * * * *', taskId: null };
const available: RouterWatchdogState = { projectPath: '/data/router/1/.tofu-watchdog', conversationId: 'coordinator-watchdog',
  revision: 1, availability: 'available', capabilities, review };
const unbound: RouterWatchdogState = { ...available, conversationId: '', revision: 0, availability: 'unbound' };
const catalog = {
  'routerWatchdog.summaryOff': '故障巡检：未开启', 'routerWatchdog.summaryOn': '故障巡检：已开启',
  'routerWatchdog.summaryPeriodic': '故障巡检：已开启 · 定期', 'routerWatchdog.enableHint': '只在出故障时运行',
  'routerWatchdog.enable': '开启故障巡检', 'routerWatchdog.review': '定期巡检',
  'routerWatchdog.schedule': '巡检频率（cron）', 'routerWatchdog.save': '保存', 'routerWatchdog.open': '打开巡检会话',
  'routerWatchdog.headerSubtitle': '负责排查故障', 'routerWatchdog.back': '返回总负责人',
  'routerWatchdog.unavailable': '故障巡检不可用：{error}', 'routerWatchdog.reviewFailed': '定期巡检未更新：{error}',
};
let release: () => void = () => {};

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  const arrived = new Promise<void>((resolve) => { release = resolve; });
  vi.stubGlobal('fetch', vi.fn(async () => {
    await arrived;
    return { ok: true, status: 200, json: async () => catalog };
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function setup(read: RouterWatchdogState | Error, options: {
  ensure?: RouterWatchdogState | Error; configure?: (request: unknown) => Promise<RouterWatchdogState>;
} = {}) {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  const i18n = await import('../i18n');
  const { createRouterWatchdogEntry } = await import('./router-watchdog');
  let active = '';
  const ensure = options.ensure ?? available;
  const ports: RouterWatchdogPorts = {
    read: vi.fn(() => (read instanceof Error ? Promise.reject(read) : Promise.resolve(read))),
    ensure: vi.fn(() => (ensure instanceof Error ? Promise.reject(ensure) : Promise.resolve(ensure))),
    configureReview: vi.fn(options.configure ?? (async () => available)),
    open: vi.fn((id: string) => { active = id; }),
    back: vi.fn(),
    activeConversationId: () => active,
    notifyError: vi.fn(),
    changed: vi.fn(),
  };
  const header = document.createElement('div');
  document.body.replaceChildren(header);
  const entry = createRouterWatchdogEntry(ports, { header });
  // The main coordinator's header hosts the settings; nothing goes to the sidebar.
  document.body.append(entry.settings);
  const settings = entry.settings;
  const view = header.querySelector('header')!;
  const [enable, save, open] = ['.router-watchdog-off button', '.router-watchdog-on button', '.router-watchdog-on button:last-of-type']
    .map(selector => settings.querySelector<HTMLButtonElement>(selector)!);
  const toggle = settings.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
  const cron = settings.querySelector<HTMLInputElement>('input.ui-input')!;
  return { i18n, ports, entry, settings, view, enable, save, open, toggle, cron,
    summary: () => settings.querySelector('summary')!.textContent,
    setActive: (id: string) => { active = id; } };
}

const settle = () => new Promise(resolve => setTimeout(resolve, 0));

async function translated(i18n: { ready(): Promise<void> }) {
  const ready = i18n.ready();
  release();
  await ready;
}

describe('Router Watchdog (health checks)', () => {
  it('created before the catalog loads, shows translated settings once i18n is ready', async () => {
    const { i18n, settings, view, summary } = await setup(available);
    // Painted from the passive read before the catalog arrived: the lookup returns its key.
    expect(summary()).toBe('routerWatchdog.summaryOn');
    await translated(i18n);
    await settle();
    expect(summary()).toBe('故障巡检：已开启');
    expect(settings.textContent).toContain('定期巡检');
    expect(settings.textContent).toContain('巡检频率（cron）');
    expect(settings.textContent).toContain('打开巡检会话');
    expect(view.textContent).toContain('负责排查故障');
    expect(view.querySelector('button')!.textContent).toBe('返回总负责人');
  });

  it('turns health checks on in place, without leaving the main coordinator', async () => {
    const { i18n, ports, entry, settings, enable, toggle, cron, summary } = await setup(unbound);
    await translated(i18n);
    await settle();
    expect(summary()).toBe('故障巡检：未开启');
    expect(settings.querySelector<HTMLElement>('.router-watchdog-off')!.hidden).toBe(false);
    expect(settings.querySelector<HTMLElement>('.router-watchdog-on')!.hidden).toBe(true);
    expect(enable.className).toContain('ui-btn');
    // The path is known from the passive read already, so presence can tell its conversation apart.
    expect(entry.location()).toBe('/data/router/1/.tofu-watchdog');
    expect(entry.conversationId()).toBe('');
    expect(ports.changed).toHaveBeenCalledTimes(1);
    enable.click();
    await settle();
    expect(ports.ensure).toHaveBeenCalledTimes(1);
    expect(ports.open).not.toHaveBeenCalled();
    // A new conversation identity is announced so lists can stop showing it.
    expect(ports.changed).toHaveBeenCalledTimes(2);
    expect(entry.conversationId()).toBe('coordinator-watchdog');
    expect(summary()).toBe('故障巡检：已开启');
    expect(settings.querySelector<HTMLElement>('.router-watchdog-off')!.hidden).toBe(true);
    expect(settings.querySelector<HTMLElement>('.router-watchdog-on')!.hidden).toBe(false);
    expect(toggle.checked).toBe(false);
    expect(cron.value).toBe('7,37 * * * *');
  });

  it('opens its conversation from the settings and marks only that conversation, with a way back', async () => {
    const { i18n, ports, entry, view, open, setActive } = await setup(available);
    await translated(i18n);
    await settle();
    expect(view.hidden).toBe(true);
    open.click();
    expect(ports.open).toHaveBeenCalledWith('coordinator-watchdog');
    entry.refresh();
    expect(view.hidden).toBe(false);
    view.querySelector('button')!.click();
    expect(ports.back).toHaveBeenCalledTimes(1);
    setActive('some-other-chat'); entry.refresh();
    expect(view.hidden).toBe(true);
  });

  it('sends the owner review choice and adopts the server state', async () => {
    const configure = vi.fn(async () => ({ ...available, review: { enabled: true, schedule: '*/15 * * * *',
      taskId: 'system-1-router-watchdog-review' } }));
    const { i18n, ports, toggle, cron, save, summary } = await setup(available, { configure });
    await translated(i18n);
    await settle();
    cron.value = ' */15 * * * * ';
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change'));
    await settle();
    expect(ports.configureReview).toHaveBeenCalledWith({ enabled: true, schedule: '*/15 * * * *' });
    expect(toggle.checked).toBe(true);
    expect(cron.value).toBe('*/15 * * * *');
    expect(summary()).toBe('故障巡检：已开启 · 定期');
    save.click();
    await settle();
    expect(ports.configureReview).toHaveBeenCalledTimes(2);
  });

  it('shows only server state for a switch change made while a request is in flight', async () => {
    let finish!: (state: RouterWatchdogState) => void;
    const configure = vi.fn(() => new Promise<RouterWatchdogState>((resolve) => { finish = resolve; }));
    const { i18n, ports, toggle, save } = await setup(available, { configure });
    await translated(i18n);
    await settle();
    save.click();
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change'));
    expect(ports.configureReview).toHaveBeenCalledTimes(1);
    expect(toggle.checked).toBe(false);
    finish(available);
    await settle();
    expect(save.disabled).toBe(false);
  });

  it('reverts the switch and shows the server refusal', async () => {
    const configure = vi.fn(async () => { throw new Error('Invalid schedule'); });
    const { i18n, toggle, settings } = await setup(available, { configure });
    await translated(i18n);
    await settle();
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change'));
    await settle();
    expect(toggle.checked).toBe(false);
    expect(settings.querySelector('.ui-field__error')!.textContent).toBe('定期巡检未更新：Invalid schedule');
  });

  it('lets another surface ensure the Watchdog and adopt its binding without opening it', async () => {
    const { i18n, ports, entry, summary } = await setup(unbound);
    await translated(i18n);
    await settle();
    await expect(entry.ensure()).resolves.toBe('coordinator-watchdog');
    expect(ports.open).not.toHaveBeenCalled();
    expect(entry.conversationId()).toBe('coordinator-watchdog');
    expect(summary()).toBe('故障巡检：已开启');
  });

  it('rejects an ensure from another surface with the localized reason', async () => {
    const refused = await setup(unbound, { ensure: new Error('home not writable') });
    await translated(refused.i18n);
    await expect(refused.entry.ensure()).rejects.toThrow('故障巡检不可用：home not writable');
    const unavailable = await setup(unbound, { ensure: unbound });
    await translated(unavailable.i18n);
    await expect(unavailable.entry.ensure()).rejects.toThrow('故障巡检不可用：unbound');
    expect(unavailable.ports.open).not.toHaveBeenCalled();
  });

  it('reports enable failures, ignores repeated clicks and cleans up on destroy', async () => {
    const { i18n, ports, entry, settings, view, enable } = await setup(new Error('offline'),
      { ensure: new Error('home not writable') });
    await translated(i18n);
    enable.click(); enable.click();
    await settle();
    expect(ports.ensure).toHaveBeenCalledTimes(1);
    expect(ports.notifyError).toHaveBeenCalledWith('故障巡检不可用：home not writable');
    expect(enable.disabled).toBe(false);
    entry.destroy();
    expect(settings.isConnected).toBe(false);
    expect(view.isConnected).toBe(false);
  });
});
