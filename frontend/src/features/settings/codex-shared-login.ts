/** Subscription-page Codex reuse card. Entry: renderCodexSharedLogin.
 * Owns DOM and single-flight feedback; credential policy stays server-side.
 * State is scoped to the mounted card, reclaimed when its DOM is removed.
 */
import { reuseCodexLogin, type CodexLoginStatus } from '../../api/codex-shared-login';
import { t, type I18nKey } from '../../i18n';
import { createButton } from '../../ui';

type State = { status?: CodexLoginStatus; busy: boolean; issue: boolean;
  refresh: (status: CodexLoginStatus) => void; paint: () => void };
const cards = new WeakMap<HTMLElement, State>();
const issueKeys: Readonly<Record<string, I18nKey>> = {
  codex_access_expired: 'settings.codexSharedExpired',
  codex_command_timeout: 'settings.codexSharedCommandFailed',
  codex_command_unavailable: 'settings.codexSharedCommandFailed',
  codex_command_invalid_response: 'settings.codexSharedCommandFailed',
  codex_command_unsupported: 'settings.codexSharedCommandFailed',
  codex_credentials_unreadable: 'settings.codexSharedUnreadable',
  codex_not_signed_in: 'settings.codexSharedMissing',
  credential_store_unsupported: 'settings.codexSharedStoreUnsupported',
};

export function renderCodexSharedLogin(status?: CodexLoginStatus, refresh?: (status: CodexLoginStatus) => void): void {
  const host = document.getElementById('oauthCodexCard');
  if (!host) return;
  let state = cards.get(host);
  if (!state) {
    const panel = document.createElement('section');
    panel.className = 'codex-shared-login';
    panel.id = 'oauthCodexSharedLogin';
    panel.setAttribute('aria-labelledby', 'oauthCodexSharedTitle');
    const icon = document.createElement('span');
    icon.className = 'codex-shared-icon';
    icon.textContent = '>_';
    icon.setAttribute('aria-hidden', 'true');
    const content = document.createElement('div');
    content.className = 'codex-shared-content';
    const heading = document.createElement('div');
    heading.className = 'codex-shared-heading';
    const title = document.createElement('strong');
    title.id = 'oauthCodexSharedTitle';
    const benefit = document.createElement('span');
    benefit.className = 'codex-shared-benefit';
    heading.append(title, benefit);
    const description = document.createElement('p');
    const actions = document.createElement('div');
    actions.className = 'codex-shared-actions';
    const feedback = document.createElement('p');
    feedback.id = 'oauthCodexSharedFeedback';
    feedback.className = 'codex-shared-feedback';
    feedback.setAttribute('role', 'status');
    const hint = document.createElement('p');
    hint.className = 'codex-shared-hint';
    const current: State = { busy: false, issue: false, refresh: () => {}, paint: () => {} };
    const button = createButton({label: t('settings.codexSharedAction'), variant: 'primary',
      onClick: async () => {
        if (current.busy || button.disabled) return;
        current.busy = true;
        current.issue = false;
        current.paint();
        try {
          const result = await reuseCodexLogin();
          if (!panel.isConnected) return;
          if (result.status !== 'success') {
            current.issue = true;
            feedback.textContent = t(issueKeys[result.auth_issue ?? ''] ?? 'settings.codexSharedUnavailable', {} as never);
          } else {
            current.status = { ...result, authenticated: true, shared_login_reuse_supported: true };
            current.refresh(current.status);
          }
        } catch {
          if (!panel.isConnected) return;
          current.issue = true;
          feedback.textContent = t('settings.codexSharedFailed');
        } finally {
          current.busy = false;
          if (panel.isConnected) current.paint();
        }
      }});
    button.id = 'oauthCodexReuseBtn';
    button.setAttribute('aria-describedby', feedback.id);
    actions.append(button);
    content.append(heading, description, actions, feedback, hint);
    panel.append(icon, content);
    host.querySelector('.oauth-provider-desc')?.after(panel);
    current.paint = () => {
      const s = current.status;
      const shared = s?.credential_source === 'local_codex' || s?.credential_source === 'desktop_codex';
      const connected = !!s?.authenticated;
      const pending = ['started', 'waiting_callback', 'exchanging'].includes(s?.status ?? '');
      title.textContent = t('settings.codexSharedTitle');
      benefit.textContent = t('settings.codexSharedBenefit');
      description.textContent = t('settings.codexSharedDescription');
      hint.textContent = t('settings.codexSharedHint');
      button.disabled = current.busy || !s?.shared_login_reuse_supported || connected || pending;
      button.textContent = current.busy ? t('settings.codexSharedChecking')
        : connected && shared ? t('settings.codexSharedConnected')
        : current.issue ? t('settings.codexSharedRetry') : t('settings.codexSharedAction');
      panel.dataset.state = current.issue ? 'error' : connected && shared ? 'connected' : 'idle';
      panel.setAttribute('aria-busy', String(current.busy));
      if (!current.issue) feedback.textContent = connected
        ? shared ? t(s?.credential_source === 'desktop_codex' ? 'settings.codexSharedRemote' : 'settings.codexSharedLocal')
          : t('settings.codexSharedOwn')
        : pending ? t('settings.codexSharedWaiting')
        : s && !s.shared_login_reuse_supported ? t('settings.codexSharedUpdate') : '';
      feedback.hidden = !feedback.textContent;
    };
    state = current;
    cards.set(host, state);
  }
  if (status) {
    state.status = status;
    if (status.authenticated) state.issue = false;
  }
  if (refresh) state.refresh = refresh;
  state.paint();
}
