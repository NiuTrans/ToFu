/** Kimi Code subscription card: device-code sign-in, status and sign-out.
 * Entry: kimiCodeLogin (retained OAuth bridges call it). Status arrives via
 * the shared subscription status read; commands use the existing device-login
 * and logout endpoints, and device polling reuses the OAuth panel lifetime.
 * The server keeps the device code; this view only shows the user code.
 */
import {featureRegistry} from '../../feature-registry';
import {t} from '../../i18n';
import {brandIconHtml} from '../../core/model-brand-icons';
import {captureOAuthPanel, type OAuthPanelLifetime} from './oauth-panel-lifetime';

const PROVIDER = 'kimi';

export interface KimiCodeStatus {
  status?: string;
  authenticated?: boolean;
  provider_ready?: boolean;
  model_count?: number;
  expire?: number | null;
  auth_issue?: string;
  device?: {user_code?: string; verification_url?: string} | null;
}
/** Retained-runtime ports, passed per call until the OAuth presenter migrates. */
export interface KimiCodePorts {
  loadStatus(fromRepoll: boolean): unknown;
  refreshCatalog(session: unknown): Promise<unknown> | null;
}
interface Globals {
  Api: {oauth: {
    deviceLoginPost(provider: string): Promise<Response | null>;
    logoutPost(provider: string): Promise<Response | null>;
  }};
  showConfirm(message: string, options?: {danger: boolean}): Promise<boolean>;
  showAlert(message: string): unknown;
}
const globals = () => featureRegistry as unknown as Globals;
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T | null;

function show(element: HTMLElement | null, visible: boolean): void {
  if (element) element.style.display = visible ? '' : 'none';
}

function expiryLabel(seconds: unknown): string {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return '';
  try {
    return new Date(value * 1000).toLocaleString(document.documentElement.lang || undefined, {
      dateStyle: 'medium', timeStyle: 'short'});
  } catch { return ''; }
}

function renderDevice(device: KimiCodeStatus['device']): void {
  const panel = byId('oauthKimiDevice');
  const visible = !!device?.user_code;
  show(panel, visible);
  if (!visible) return;
  const code = byId('oauthKimiDeviceCode');
  if (code) code.textContent = device?.user_code || '';
  const link = byId<HTMLAnchorElement>('oauthKimiDeviceLink');
  const url = device?.verification_url || '';
  if (link && url.startsWith('https://')) link.href = url;
}

function observeDevice(view: OAuthPanelLifetime, ports: KimiCodePorts): void {
  view.startDevicePoll(PROVIDER, () => { ports.loadStatus(true); }, () => {
    void ports.refreshCatalog(view.session)?.then(() => { if (view.isCurrent()) ports.loadStatus(false); });
  });
}

/** Paint the card; with ports, also resume or retire this provider's device poll. */
function render(status: KimiCodeStatus | undefined, ports?: KimiCodePorts): void {
  const badge = byId('oauthKimiStatus');
  if (!badge || !status) return;
  const icon = byId('oauthKimiIcon');
  if (icon && !icon.childElementCount) icon.innerHTML = brandIconHtml('kimi', 20);
  const login = byId<HTMLButtonElement>('oauthKimiLoginBtn');
  const logout = byId<HTMLButtonElement>('oauthKimiLogoutBtn');
  const info = byId('oauthKimiInfo');
  const waiting = !status.authenticated
    && (status.status === 'started' || status.status === 'waiting_callback');
  renderDevice(waiting ? status.device : null);
  const view = ports ? captureOAuthPanel() : null;
  if (view && ports && waiting && status.device?.user_code) observeDevice(view, ports);
  else if (view && !waiting) view.stopDevicePoll(PROVIDER);
  badge.title = '';
  if (status.authenticated) {
    const ready = status.provider_ready !== false;
    badge.textContent = ready ? t('settings.oauthModelsReady', {n: status.model_count || 0})
      : t('settings.oauthAutomaticRecovery');
    badge.className = 'oauth-status-badge ' + (ready ? 'authenticated' : 'pending');
    const expiry = byId('oauthKimiExpiry');
    const label = expiryLabel(status.expire);
    if (expiry) expiry.textContent = label ? t('settings.oauthKimiExpires', {time: label})
      : t('settings.oauthConnectedAccount');
    show(info, true);
  } else {
    show(info, false);
    if (waiting) {
      badge.textContent = t('settings.oauthWaitingAuth');
      badge.className = 'oauth-status-badge pending';
    } else if (status.status === 'error') {
      badge.textContent = t('settings.oauthError');
      badge.className = 'oauth-status-badge error';
    } else if (status.auth_issue) {
      badge.textContent = t('settings.oauthKimiSignInAgain');
      badge.className = 'oauth-status-badge error';
    } else {
      badge.textContent = t('settings.oauthNotLoggedIn');
      badge.className = 'oauth-status-badge';
    }
  }
  show(login, !status.authenticated);
  show(logout, !!status.authenticated);
  if (login && !waiting) {
    login.disabled = false;
    login.textContent = t('settings.oauthDeviceLogin');
  }
}

async function errorText(response: Response | null): Promise<string> {
  if (!response) return 'network';
  try {
    const body = await response.json() as {error?: unknown; detail?: unknown};
    const error = body?.error;
    if (typeof error === 'string') return error;
    if (error && typeof error === 'object' && 'message' in error) return String((error as {message: unknown}).message);
    return String(body?.detail || `HTTP ${response.status}`);
  } catch { return `HTTP ${response.status}`; }
}

async function start(ports: KimiCodePorts): Promise<void> {
  const view = captureOAuthPanel();
  const login = byId<HTMLButtonElement>('oauthKimiLoginBtn');
  if (!view || login?.disabled) return;
  view.invalidateStatus();
  if (login) { login.disabled = true; login.textContent = t('settings.oauthPreparing'); }
  const response = await globals().Api.oauth.deviceLoginPost(PROVIDER).catch(() => null);
  if (!view.isCurrent()) return;
  const body = response?.ok ? await response.json().catch(() => null) as
    {status?: string; user_code?: string; verification_url?: string; error?: unknown} | null : null;
  if (!body?.user_code || body.error) {
    if (login) { login.disabled = false; login.textContent = t('settings.oauthDeviceLogin'); }
    globals().showAlert(t('settings.oauthLoginFailed', {
      error: body?.error ? String(body.error) : await errorText(response)}));
    return;
  }
  render({status: 'waiting_callback', authenticated: false,
          device: {user_code: body.user_code, verification_url: body.verification_url}}, ports);
}

async function logout(ports: KimiCodePorts): Promise<void> {
  const view = captureOAuthPanel();
  if (!view) return;
  if (!await globals().showConfirm(t('settings.oauthLogoutConfirm', {provider: 'Kimi Code'}))) return;
  if (!view.isCurrent()) return;
  view.invalidateStatus();
  view.stopDevicePoll(PROVIDER);
  const response = await globals().Api.oauth.logoutPost(PROVIDER).catch(() => null);
  if (!response?.ok) {
    if (view.isCurrent()) globals().showAlert(t('settings.oauthLogoutFailed', {error: await errorText(response)}));
    return;
  }
  if (view.isCurrent()) render({status: 'not_started', authenticated: false});
  await ports.refreshCatalog(view.session);
}

async function copyCode(button: HTMLElement): Promise<void> {
  const view = captureOAuthPanel();
  const code = byId('oauthKimiDeviceCode')?.textContent || '';
  if (!view || !code || !navigator.clipboard?.writeText) return;
  await navigator.clipboard.writeText(code).catch(() => undefined);
  if (!view.isCurrent()) return;
  button.textContent = t('settings.oauthCopied');
  view.scope.timeout(() => { button.textContent = t('settings.oauthCopyCode'); }, 1500);
}

export const kimiCodeLogin = {render, start, logout, copyCode};
