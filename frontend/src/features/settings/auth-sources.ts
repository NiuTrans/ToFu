import { featureRegistry } from '../../feature-registry';
import { escapeHtmlText as escape } from '../../html-safety';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import type { I18nKey } from '../../i18n';

interface AuthSourceField {
  name?: string;
  importance?: string;
}

interface AuthSourceKnowledge {
  pinned?: boolean;
  version?: string | number;
}

interface AuthSourceRow {
  domain?: string;
  label?: string;
  enabled?: boolean;
  has_cookies?: boolean;
  cookie_count?: number;
  has_proxy?: boolean;
  proxy_hint?: string;
  login_url?: string;
  risk_note_key?: I18nKey;
  access_strategy?: string;
  knowledge?: AuthSourceKnowledge;
  fields?: AuthSourceField[];
}

interface LiveSessionStatus {
  extension?: boolean;
  live_session?: boolean;
}

interface AuthSourcesApi {
  list(): Promise<{ sources?: AuthSourceRow[] } | null>;
  liveSession(domain: string): Promise<LiveSessionStatus | null>;
  toggle(domain: string, enabled: boolean): Promise<unknown>;
  upsert(body: {
    domain: string;
    cookie_fields: Record<string, string>;
    proxy: string;
    enabled: true;
  }): Promise<unknown>;
  remove(domain: string): Promise<unknown>;
}

type AuthSourcesWindow = Window & {
  Api?: { authSources?: AuthSourcesApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  _renderAuthSources?: () => void;
  _authSourceTogglePanel?: (domain: string) => void;
  _authSourceOpenLogin?: (domain: string, url?: string) => void;
  _authSourceToggle?: (domain: string, enabled: boolean) => void;
  _authSourceCollectFields?: (id: string) => CollectedFields;
  _authSourceSavePaste?: (domain: string) => void;
  _authSourceDisconnect?: (domain: string) => void;
  _destroyAuthSources?: () => void;
};

interface CollectedFields {
  values: Record<string, string>;
  missing: string[];
}

let viewScope: LifecycleScope | null = null;
let renderGeneration = 0;

function globals(): AuthSourcesWindow {
  return featureRegistry as unknown as AuthSourcesWindow;
}

function authSourcesApi(): AuthSourcesApi {
  const api = globals().Api?.authSources;
  if (!api) throw new Error('Auth-sources API is not ready');
  return api;
}

function translate(
  key: I18nKey,
  fallback: string,
  values?: Record<string, unknown>,
): string {
  return globals().t?.(key, values) || fallback;
}

export function authSourceDomId(domain: string): string {
  return String(domain).replace(/[^a-zA-Z0-9]/g, '_');
}

function riskNoteHtml(source: AuthSourceRow): string {
  const key = source.risk_note_key || '';
  if (!key) return '';
  return `<div class="auth-src-risk-note">${escape(translate(key, key))}</div>`;
}

function registryBadgesHtml(source: AuthSourceRow): string {
  const strategy = source.access_strategy || 'browser_first';
  const strategyKey = ({
    browser_first: 'settings.authSrcStrategyBrowserFirst',
    cookies_replay: 'settings.authSrcStrategyCookiesReplay',
    public: 'settings.authSrcStrategyPublic',
  } satisfies Record<string, I18nKey>)[strategy]
    || 'settings.authSrcStrategyBrowserFirst';
  const knowledge = source.knowledge || {};
  let knowledgeHtml = '';
  if (knowledge.pinned) {
    knowledgeHtml = `<span class="auth-src-meta-badge knowledge">${escape(
      translate('settings.authSrcKnowledgePinned', '已内化 v{v}')
        .replace('{v}', String(knowledge.version || '?')),
    )}</span>`;
  } else if (source.has_cookies) {
    knowledgeHtml = `<span class="auth-src-meta-badge">${escape(
      translate('settings.authSrcKnowledgeCredentials', '仅凭据'),
    )}</span>`;
  }
  const id = authSourceDomId(source.domain || '');
  return `<div class="auth-src-badges">
    <span class="auth-src-meta-badge strategy">${escape(
      translate(strategyKey, strategyKey),
    )}</span>${knowledgeHtml}<span id="authSrcLive_${escape(id)}"></span>
  </div>`;
}

export function authSourceFieldRowsHtml(
  source: AuthSourceRow,
  id: string,
): string {
  const fields = source.fields?.length
    ? source.fields
    : [{ name: 'cookie', importance: 'required' }];
  return fields.map((field, index) => {
    const name = field.name || '';
    const importance = field.importance || 'optional';
    const badge = importance === 'required'
      ? translate('settings.authSrcRequired', '必填')
      : importance === 'recommended'
        ? translate('settings.authSrcRecommended', '建议填写')
        : translate('settings.authSrcOptional', '可选');
    const fieldId = `authSrcField_${id}_${index}`;
    const placeholder = translate(
      'settings.authSrcFieldPh', '粘贴 {name} 的值').replace('{name}', name);
    return `<div class="auth-src-field">
      <label class="auth-src-field-label" for="${escape(fieldId)}">
        <code>${escape(name)}</code>
        <span class="auth-src-field-badge ${escape(importance)}">${escape(badge)}</span>
      </label>
      <input type="text" class="auth-src-field-input" spellcheck="false" autocomplete="off"
             id="${escape(fieldId)}" data-cookie-name="${escape(name)}"
             data-importance="${escape(importance)}" placeholder="${escape(placeholder)}">
    </div>`;
  }).join('');
}

function connectPanelHtml(source: AuthSourceRow, domain: string, id: string): string {
  const loginUrl = source.login_url || '';
  const stepOne = loginUrl
    ? `<li><span class="auth-src-step-txt">${escape(translate(
      'settings.authSrcStep1', '在你自己的浏览器中打开该站点并登录'))}</span>
       <button type="button" class="auth-src-btn sm" data-auth-action="open-login"
               data-domain="${escape(domain)}" data-url="${escape(loginUrl)}">${escape(
                 translate('settings.authSrcOpenLogin', '打开登录页 ↗'))}</button></li>`
    : `<li><span class="auth-src-step-txt">${escape(translate(
      'settings.authSrcStep1Generic', '在你自己的浏览器中登录该站点'))}</span></li>`;
  const browserFirst = (source.access_strategy || 'browser_first')
    === 'browser_first';
  const liveHint = browserFirst
    ? `<div class="auth-src-live-hint">${escape(translate(
      'settings.authSrcBrowserFirstHint',
      '通常无需粘贴 Cookie：在你自己的浏览器里登录该站即可——检测到浏览器会话后直接启用，搜索与抓取就走你的活会话。下面粘贴 Cookie 只是浏览器不在线时的离线兜底。',
    ))}</div>`
    : '';
  const fieldSteps = browserFirst
    ? `<li>${escape(translate(
      'settings.authSrcStep2FieldsFallback',
      '（离线兜底，可选）浏览器不在线时才需要：F12 → Application → Cookies，逐个复制 Cookie 值粘贴到下面',
    ))}</li>`
    : `<li>${escape(translate(
      'settings.authSrcStep2Fields',
      '打开开发者工具 (F12) → Application → Cookies，找到下面每个 Cookie，逐个复制它的 Value',
    ))}</li><li>${escape(translate(
      'settings.authSrcStep3Fields',
      '分别粘贴到对应输入框并保存（只填值，不要带名字或分号）',
    ))}</li>`;
  return `<div class="auth-src-panel" id="authSrcPanel_${escape(id)}" style="display:none">
    ${riskNoteHtml(source)}${liveHint}
    <ol class="auth-src-steps">${stepOne}${fieldSteps}</ol>
    <div class="auth-src-fields">${authSourceFieldRowsHtml(source, id)}</div>
    <input type="text" class="auth-src-proxy" id="authSrcProxy_${escape(id)}"
           placeholder="${escape(translate(
             'settings.authSrcProxyPh', '可选代理，例如 http://host:port'))}">
    <div class="auth-src-panel-actions">
      <button type="button" class="auth-src-btn primary" data-auth-action="save"
              data-domain="${escape(domain)}">${escape(translate(
                'settings.authSrcSaveConnect', '保存并连接'))}</button>
      <button type="button" class="auth-src-btn ghost" data-auth-action="toggle-panel"
              data-domain="${escape(domain)}">${escape(translate(
                'common.cancel', '取消'))}</button>
    </div>
  </div>`;
}

export function authSourceCardHtml(source: AuthSourceRow): string {
  const connected = Boolean(source.has_cookies);
  const enabled = Boolean(source.enabled);
  const domain = source.domain || '';
  const id = authSourceDomId(domain);
  const strategy = source.access_strategy || 'browser_first';
  const toggleAllowed = connected || strategy !== 'cookies_replay';
  let stateClass = 'off';
  let stateText = translate('settings.authSrcNotConnected', '未连接');
  if (connected && enabled) {
    stateClass = 'on';
    stateText = `${translate('settings.authSrcConnected', '已连接')} · ${
      source.cookie_count || 0} cookies${source.has_proxy
      ? ` · proxy ${source.proxy_hint || ''}`
      : ''}`;
  } else if (connected) {
    stateClass = 'paused';
    stateText = translate('settings.authSrcDisabled', '已连接（已停用）');
  }
  const primaryClass = connected ? 'auth-src-btn' : 'auth-src-btn primary';
  const primaryText = connected
    ? translate('settings.authSrcReconnect', '重新连接')
    : translate('settings.authSrcConnect', '连接');
  const disconnect = connected
    ? `<button type="button" class="auth-src-btn ghost danger"
              data-auth-action="disconnect" data-domain="${escape(domain)}">${escape(
                translate('settings.authSrcDisconnectBtn', '断开'))}</button>`
    : '';
  return `<div class="auth-src-card ${stateClass}" data-domain="${escape(domain)}">
    <div class="auth-src-row">
      <span class="auth-src-state-dot ${stateClass}"></span>
      <div class="auth-src-meta">
        <div class="auth-src-name">${escape(source.label || domain)}<span class="auth-src-domain">${escape(domain)}</span></div>
        <div class="auth-src-state-text">${escape(stateText)}</div>
        ${registryBadgesHtml(source)}${riskNoteHtml(source)}
      </div>
      <label class="auth-src-switch" title="${escape(translate(
        'settings.authSrcToggle', '启用 / 停用'))}">
        <input type="checkbox" data-auth-action="toggle" data-domain="${escape(domain)}"
               ${enabled ? 'checked' : ''} ${toggleAllowed ? '' : 'disabled'}>
        <span class="auth-src-switch-track"><span class="auth-src-switch-thumb"></span></span>
      </label>
    </div>
    <div class="auth-src-actions">
      <button type="button" class="${primaryClass}" data-auth-action="toggle-panel"
              data-domain="${escape(domain)}">${escape(primaryText)}</button>${disconnect}
    </div>
    ${connectPanelHtml(source, domain, id)}
    <div class="auth-src-msg" id="authSrcMsg_${escape(id)}"></div>
  </div>`;
}

function wireRoot(root: HTMLElement, scope: LifecycleScope): void {
  scope.listen(root, 'click', (event) => {
    const target = event.target instanceof Element
      ? event.target.closest<HTMLElement>('[data-auth-action]')
      : null;
    if (!target || target instanceof HTMLInputElement) return;
    const domain = target.dataset.domain || '';
    switch (target.dataset.authAction) {
      case 'toggle-panel':
        authSourceTogglePanel(domain);
        break;
      case 'open-login':
        authSourceOpenLogin(domain, target.dataset.url || '');
        break;
      case 'save':
        void authSourceSavePaste(domain);
        break;
      case 'disconnect':
        void authSourceDisconnect(domain);
        break;
    }
  });
  scope.listen(root, 'change', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)
        || target.dataset.authAction !== 'toggle') return;
    void authSourceToggle(target.dataset.domain || '', target.checked);
  });
}

function probeLiveSessions(
  sources: readonly AuthSourceRow[],
  generation: number,
): void {
  for (const source of sources) {
    if ((source.access_strategy || 'browser_first') !== 'browser_first') continue;
    const domain = source.domain || '';
    void authSourcesApi().liveSession(domain).then((status) => {
      if (generation !== renderGeneration || !status) return;
      const element = document.getElementById(
        `authSrcLive_${authSourceDomId(domain)}`);
      if (!(element instanceof HTMLElement)) return;
      let className = 'auth-src-meta-badge live';
      let text = translate(
        'settings.authSrcLiveNone', '未检测到浏览器登录');
      if (!status.extension) {
        className += ' off';
        text = translate('settings.authSrcLiveOffline', '扩展离线');
      } else if (status.live_session) {
        className += ' on';
        text = translate('settings.authSrcLiveOn', '浏览器会话已检测');
      }
      const badge = document.createElement('span');
      badge.className = className;
      badge.textContent = text;
      element.replaceChildren(badge);
    }).catch((error: unknown) => {
      if (generation === renderGeneration) {
        console.debug('[AuthSrc] live-session probe failed', error);
      }
    });
  }
}

export function destroyAuthSources(): void {
  renderGeneration += 1;
  viewScope?.destroy();
  viewScope = null;
}

export async function renderAuthSources(): Promise<void> {
  const root = document.getElementById('authSourcesList');
  if (!(root instanceof HTMLElement)) return;
  destroyAuthSources();
  const generation = renderGeneration;
  const scope = createLifecycleScope();
  viewScope = scope;
  const loading = document.createElement('div');
  loading.className = 'auth-src-loading';
  loading.textContent = translate('common.loading', '加载中…');
  root.replaceChildren(loading);
  try {
    const response = await authSourcesApi().list();
    if (generation !== renderGeneration || viewScope !== scope
        || document.getElementById('authSourcesList') !== root) return;
    const sources = response?.sources ?? [];
    if (!sources.length) {
      const empty = document.createElement('div');
      empty.className = 'auth-src-empty';
      empty.textContent = translate(
        'settings.authSourcesEmpty', '暂无可登录的来源。');
      root.replaceChildren(empty);
      return;
    }
    root.innerHTML = sources.map(authSourceCardHtml).join('');
    wireRoot(root, scope);
    probeLiveSessions(sources, generation);
  } catch (error: unknown) {
    if (generation !== renderGeneration || viewScope !== scope
        || document.getElementById('authSourcesList') !== root) return;
    console.warn('[AuthSrc] list failed', error);
    const empty = document.createElement('div');
    empty.className = 'auth-src-empty';
    empty.textContent = translate(
      'settings.authSourcesLoadFail', '加载失败');
    root.replaceChildren(empty);
  }
}

export function authSourceSetMessage(
  domain: string,
  text: string,
  kind = '',
): void {
  const element = document.getElementById(
    `authSrcMsg_${authSourceDomId(domain)}`);
  if (!(element instanceof HTMLElement)) return;
  element.textContent = text;
  element.className = `auth-src-msg${kind ? ` ${kind}` : ''}`;
}

export function authSourceTogglePanel(domain: string): void {
  const panel = document.getElementById(
    `authSrcPanel_${authSourceDomId(domain)}`);
  if (panel instanceof HTMLElement) {
    panel.style.display = panel.style.display === 'none' ? '' : 'none';
  }
}

export function authSourceOpenLogin(domain: string, url = ''): void {
  window.open(url || `https://${domain}/`, '_blank', 'noopener');
}

export async function authSourceToggle(
  domain: string,
  enabled: boolean,
): Promise<void> {
  const generation = renderGeneration;
  try {
    await authSourcesApi().toggle(domain, enabled);
    if (generation === renderGeneration) await renderAuthSources();
  } catch (error: unknown) {
    if (generation !== renderGeneration) return;
    const message = error instanceof Error ? error.message : String(error);
    authSourceSetMessage(domain, message, 'err');
  }
}

export function authSourceCollectFields(id: string): CollectedFields {
  const values: Record<string, string> = {};
  const missing: string[] = [];
  const inputs = document.querySelectorAll<HTMLInputElement>(
    `#authSrcPanel_${id} .auth-src-field-input`);
  for (const input of inputs) {
    const name = input.dataset.cookieName || '';
    const value = input.value.trim();
    if (value.includes('=')) {
      for (const rawPair of value.split(';')) {
        const pair = rawPair.trim();
        const equals = pair.indexOf('=');
        if (equals <= 0) continue;
        values[pair.slice(0, equals).trim()] = pair.slice(equals + 1).trim();
      }
    } else if (value) {
      values[name] = value;
    }
    if (!value && input.dataset.importance === 'required') missing.push(name);
  }
  return { values, missing };
}

export async function authSourceSavePaste(domain: string): Promise<void> {
  const generation = renderGeneration;
  const id = authSourceDomId(domain);
  const collected = authSourceCollectFields(id);
  const proxyInput = document.getElementById(`authSrcProxy_${id}`);
  const proxy = proxyInput instanceof HTMLInputElement ? proxyInput.value : '';
  if (collected.missing.length) {
    authSourceSetMessage(
      domain,
      `${translate('settings.authSrcFieldMissing', '请填写必填 Cookie：')}${
        collected.missing.join(', ')}`,
      'err',
    );
    return;
  }
  authSourceSetMessage(domain, translate('common.saving', '保存中…'));
  try {
    await authSourcesApi().upsert({
      domain,
      cookie_fields: collected.values,
      proxy,
      enabled: true,
    });
    if (generation !== renderGeneration) return;
    authSourceSetMessage(
      domain, translate('settings.authSrcSaved', '已连接'), 'ok');
    await renderAuthSources();
  } catch (error: unknown) {
    if (generation !== renderGeneration) return;
    let detail = error instanceof Error ? error.message : String(error);
    if (error && typeof error === 'object' && 'body' in error) {
      const body = (error as { body?: { error?: unknown } }).body;
      if (body?.error) detail = String(body.error);
    }
    authSourceSetMessage(
      domain,
      `${translate('settings.authSrcSaveFail', '保存失败: ')}${detail}`,
      'err',
    );
  }
}

export async function authSourceDisconnect(domain: string): Promise<void> {
  if (!window.confirm(translate(
    'settings.authSrcDisconnectConfirm', '断开并清除该来源的 Cookie？'))) return;
  const generation = renderGeneration;
  try {
    await authSourcesApi().remove(domain);
    if (generation === renderGeneration) await renderAuthSources();
  } catch (error: unknown) {
    if (generation !== renderGeneration) return;
    authSourceSetMessage(
      domain,
      error instanceof Error ? error.message : String(error),
      'err',
    );
  }
}

const bridge = globals();
bridge._renderAuthSources = () => { void renderAuthSources(); };
bridge._authSourceTogglePanel = authSourceTogglePanel;
bridge._authSourceOpenLogin = authSourceOpenLogin;
bridge._authSourceToggle = (domain, enabled) => {
  void authSourceToggle(domain, enabled);
};
bridge._authSourceCollectFields = authSourceCollectFields;
bridge._authSourceSavePaste = (domain) => { void authSourceSavePaste(domain); };
bridge._authSourceDisconnect = (domain) => {
  void authSourceDisconnect(domain);
};
bridge._destroyAuthSources = destroyAuthSources;
