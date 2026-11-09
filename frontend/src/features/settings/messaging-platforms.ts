/**
 * 消息平台 Settings tab: every chat platform in one collapsible group.
 * Responsibility: the platform sections (飞书, 大象, GitHub, 企业微信, QQ, 微信)
 * of the `messaging` panel, each collapsed by default with a one-line status
 * (connected, connecting, needs attention, disabled, not set up); expanding a
 * section shows that platform's own panel, which keeps its existing owner
 * (the retained 飞书 fields, dx-channel.ts, github-channel.ts, bot-channel.ts)
 * and its first-visit population (tab-activation.ts). The platform panels
 * stay separate fragments with their own `settingsTab_<platform>` ids; on the
 * first visit they are moved into their sections once.
 * Deep links keep working: `switchSettingsTab('<platform>')` (setting:dx,
 * setting:feishu, …) opens this tab and expands that platform.
 * Entry points: MESSAGING_PLATFORMS, isMessagingPlatform, populateMessagingTab
 * (registered as `_populateMessagingTab`), openMessagingPlatform, summarize.
 * Dependencies: the `Api.channels` facade, the coalesced server config read
 * (飞书 status), tab-activation, panel-read-session, i18n.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import type { AccountState } from '../../api/channels.generated';
import type { ChannelsEndpoints } from '../../api/endpoint-registry/channels';
import { currentSettingsReadSession } from './panel-read-session';
import { loadServerConfig } from './server-config-reader';
import { activateIndependentSettingsTab } from './tab-activation';

export const MESSAGING_PLATFORMS = ['feishu', 'dx', 'github', 'wecom', 'qq', 'wechat'] as const;
export type MessagingPlatform = typeof MESSAGING_PLATFORMS[number];
export type PlatformSummary = 'connected' | 'connecting' | 'attention' | 'disabled' | 'notSetUp';

const CONNECTING = new Set<AccountState>(['starting', 'authorizing', 'disconnected']);

interface SummaryAccount {
  enabled: boolean;
  status: { state: AccountState };
}

export function isMessagingPlatform(tab: string): tab is MessagingPlatform {
  return (MESSAGING_PLATFORMS as readonly string[]).includes(tab);
}

/** One line per platform: what its account needs, if anything. */
export function summarize(account: SummaryAccount, configured: boolean): PlatformSummary {
  if (account.enabled) {
    if (account.status.state === 'connected') return 'connected';
    return CONNECTING.has(account.status.state) ? 'connecting' : 'attention';
  }
  return configured ? 'disabled' : 'notSetUp';
}

function section(platform: MessagingPlatform): HTMLDetailsElement | null {
  const node = document.getElementById(`msgPlatform_${platform}`);
  return node instanceof HTMLDetailsElement ? node : null;
}

/** Move each platform's panel into its section and bind its expansion, once. */
export function mountMessagingPlatforms(): void {
  for (const platform of MESSAGING_PLATFORMS) {
    const body = document.getElementById(`msgPlatformBody_${platform}`);
    const panel = document.getElementById(`settingsTab_${platform}`);
    if (body && panel && panel.parentElement !== body) body.appendChild(panel);
    const details = section(platform);
    if (details && !details.dataset.bound) {
      details.dataset.bound = 'true';
      details.addEventListener('toggle', () => {
        if (details.open) activateIndependentSettingsTab(platform);
      });
    }
  }
}

function setStatus(platform: MessagingPlatform, summary: PlatformSummary): void {
  const node = document.getElementById(`msgPlatformStatus_${platform}`);
  if (!node) return;
  node.dataset.state = summary;
  node.textContent = t(`settings.messaging.status.${summary}` as const);
}

function channelsApi(): ChannelsEndpoints | null {
  return (featureRegistry.Api as { channels?: ChannelsEndpoints } | undefined)?.channels ?? null;
}

function refreshStatuses(): void {
  const session = currentSettingsReadSession();
  const current = () => !session || session.isCurrent();
  const signal = session?.scope.signal;
  const apply = (platform: MessagingPlatform, read: Promise<PlatformSummary>) => {
    void read.then((summary) => { if (current()) setStatus(platform, summary); }, () => undefined);
  };
  void loadServerConfig(session).then((cfg) => {
    if (!cfg || !current()) return;
    const feishu = (cfg.feishu ?? {}) as { connected?: boolean; enabled?: boolean; app_id?: string };
    setStatus('feishu', feishu.connected ? 'connected' : feishu.enabled ? 'attention'
      : feishu.app_id ? 'disabled' : 'notSetUp');
  });
  const api = channelsApi();
  if (!api) return;
  apply('dx', api.dxAccount(signal).then((account) => summarize(account, !!(account.identity || account.loginMis))));
  apply('github', api.githubAccount(signal).then((account) => summarize(account, !!account.credential)));
  for (const platform of ['wecom', 'qq', 'wechat'] as const) {
    apply(platform, api.botAccount(platform, signal).then(
      (account) => summarize(account, !!(account.credential || account.appId))));
  }
}

export function populateMessagingTab(): void {
  mountMessagingPlatforms();
  refreshStatuses();
}

/** Expand one platform of the (already active) messaging tab and bring it into view. */
export function openMessagingPlatform(platform: MessagingPlatform): void {
  mountMessagingPlatforms();
  const details = section(platform);
  if (!details) return;
  if (details.open) activateIndependentSettingsTab(platform);
  else details.open = true; // its toggle event activates the panel
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  details.scrollIntoView?.({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
}

(featureRegistry as unknown as { _populateMessagingTab?: () => void })._populateMessagingTab = populateMessagingTab;
