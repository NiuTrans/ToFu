// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* The delivered 消息平台 panel (static/settings_panels/ is generated from
 * frontend/src/settings-panels/), so the spec pins the markup the tab ships. */
import messagingPanel from '../../../../static/settings_panels/messaging.html?raw';

const runtime = vi.hoisted(() => ({}) as Record<string, unknown>);
vi.mock('../../feature-registry', () => ({ featureRegistry: runtime }));
vi.mock('../../i18n', () => ({ t: (key: string) => key }));

import {
  MESSAGING_PLATFORMS, isMessagingPlatform, openMessagingPlatform, populateMessagingTab, summarize,
} from './messaging-platforms';
import { beginSettingsReadSession, retireSettingsReads } from './panel-read-session';

type State = 'disabled' | 'starting' | 'authorizing' | 'connected' | 'disconnected' | 'needs_login'
  | 'runtime_unavailable' | 'kicked' | 'error';
const status = (state: State) => ({ state, detail: '', updatedAtMs: 1, connectedAtMs: null });

function fakeApi() {
  return {
    serverConfig: { get: vi.fn(async () => ({ feishu: { enabled: true, app_id: 'cli_a', connected: true } })) },
    channels: {
      dxAccount: vi.fn(async () => ({ enabled: true, status: status('authorizing'), identity: null, loginMis: 'me' })),
      githubAccount: vi.fn(async () => ({ enabled: false, status: status('disabled'), credential: null })),
      botAccount: vi.fn(async (channel: string) => ({
        wecom: { enabled: true, status: status('needs_login'), credential: { name: 'n' }, appId: 'bot' },
        qq: { enabled: false, status: status('disabled'), credential: null, appId: '1024' },
        wechat: { enabled: false, status: status('disabled'), credential: null, appId: null },
      })[channel]),
    },
  };
}

async function settle() {
  for (let index = 0; index < 6; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function section(platform: string): HTMLDetailsElement {
  return document.getElementById(`msgPlatform_${platform}`) as HTMLDetailsElement;
}

function chip(platform: string): HTMLElement {
  return document.getElementById(`msgPlatformStatus_${platform}`)!;
}

describe('summarize', () => {
  const account = (enabled: boolean, state: State) => ({ enabled, status: { state } });

  it('says what an enabled account needs', () => {
    expect(summarize(account(true, 'connected'), true)).toBe('connected');
    for (const state of ['starting', 'authorizing', 'disconnected'] as const) {
      expect(summarize(account(true, state), true)).toBe('connecting');
    }
    for (const state of ['needs_login', 'runtime_unavailable', 'kicked', 'error'] as const) {
      expect(summarize(account(true, state), true)).toBe('attention');
    }
  });

  it('tells a configured but disabled platform from one never set up', () => {
    expect(summarize(account(false, 'disabled'), true)).toBe('disabled');
    expect(summarize(account(false, 'disabled'), false)).toBe('notSetUp');
  });
});

describe('isMessagingPlatform', () => {
  it('knows the six chat platforms and nothing else', () => {
    expect(MESSAGING_PLATFORMS).toEqual(['feishu', 'dx', 'github', 'wecom', 'qq', 'wechat']);
    for (const platform of MESSAGING_PLATFORMS) expect(isMessagingPlatform(platform)).toBe(true);
    for (const tab of ['messaging', 'mcp', 'general', '']) expect(isMessagingPlatform(tab)).toBe(false);
  });
});

describe('消息平台 tab', () => {
  let populated: string[];

  beforeEach(() => {
    populated = [];
    for (const key of Object.keys(runtime)) delete runtime[key];
    for (const name of ['Dx', 'GitHub', 'Wecom', 'Qq', 'Wechat']) {
      runtime[`_populate${name}Tab`] = () => populated.push(name);
      runtime[`_destroy${name}Tab`] = () => undefined;
    }
    runtime.Api = fakeApi();
    // The settings shell: the 消息平台 panel plus each platform's own panel fragment, as delivered.
    document.body.innerHTML = messagingPanel + MESSAGING_PLATFORMS
      .map((platform) => `<div class="settings-tab-panel" id="settingsTab_${platform}"><p>${platform} panel</p></div>`)
      .join('');
    beginSettingsReadSession();
  });
  afterEach(() => retireSettingsReads());

  it('ships one collapsed section per platform, in order, each with a name and a status line', () => {
    const sections = [...document.querySelectorAll<HTMLDetailsElement>('#messagingPlatforms > details')];
    expect(sections.map((node) => node.dataset.platform)).toEqual([...MESSAGING_PLATFORMS]);
    for (const node of sections) {
      expect(node.open).toBe(false);
      expect(node.querySelector('summary .msg-platform-name')!.getAttribute('data-i18n')).toMatch(/^settings\.tab/);
      expect(node.querySelector('summary .msg-platform-status')!.getAttribute('aria-live')).toBe('polite');
    }
  });

  it('moves every platform panel into its section once and shows each status without opening any', async () => {
    populateMessagingTab();
    await settle();
    for (const platform of MESSAGING_PLATFORMS) {
      const panel = document.getElementById(`settingsTab_${platform}`)!;
      expect(panel.parentElement!.id).toBe(`msgPlatformBody_${platform}`);
      expect(section(platform).open).toBe(false);
    }
    expect([...MESSAGING_PLATFORMS].map((platform) => chip(platform).dataset.state))
      .toEqual(['connected', 'connecting', 'notSetUp', 'attention', 'disabled', 'notSetUp']);
    expect(chip('wecom').textContent).toBe('settings.messaging.status.attention');
    // Collapsed sections cost one status read each, never a panel population.
    expect(populated).toEqual([]);

    populateMessagingTab();
    await settle();
    expect(document.querySelectorAll('[id="settingsTab_dx"]')).toHaveLength(1);
    expect(document.getElementById('msgPlatformBody_dx')!.children).toHaveLength(1);
  });

  it('expanding a section populates that platform panel; a deep link opens and re-reads it', async () => {
    populateMessagingTab();
    section('qq').open = true;
    await settle();
    expect(populated).toEqual(['Qq']);

    openMessagingPlatform('wecom');
    await settle();
    expect(section('wecom').open).toBe(true);
    expect(populated).toEqual(['Qq', 'Wecom']);

    // An already open section re-reads its panel on the next deep link (refreshOnVisit).
    openMessagingPlatform('wecom');
    await settle();
    expect(populated).toEqual(['Qq', 'Wecom', 'Wecom']);
  });

  it('a deep link before the first visit still mounts the panels', async () => {
    openMessagingPlatform('dx');
    await settle();
    expect(document.getElementById('settingsTab_dx')!.parentElement!.id).toBe('msgPlatformBody_dx');
    expect(section('dx').open).toBe(true);
    expect(populated).toEqual(['Dx']);
  });

  it('drops status answers that arrive after Settings closed, and a failed read leaves the line empty', async () => {
    const api = fakeApi();
    let answer!: (value: unknown) => void;
    api.channels.githubAccount = vi.fn(() => new Promise((resolve) => { answer = resolve; })) as never;
    api.channels.botAccount = vi.fn(async () => { throw new Error('offline'); }) as never;
    runtime.Api = api;
    populateMessagingTab();
    await settle();
    expect(chip('wecom').textContent).toBe('');
    retireSettingsReads();
    answer({ enabled: true, status: status('connected'), credential: { name: 'n' } });
    await settle();
    expect(chip('github').textContent).toBe('');
    expect(chip('github').dataset.state).toBe('unknown');
  });
});
