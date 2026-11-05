/** Catpaw Settings adapter: existing controls and typed API facades connect to
 * one connection lifetime. Shared routing/Provider owners retain write/CAS,
 * confirmation-target and complete-response adoption responsibilities.
 */
import {featureRegistry} from '../../feature-registry';
import {t} from '../../i18n';
import {createLifecycleScope} from '../../lifecycle';
import type {ModelRoutingDocument, ProviderDeleteResponse, CatpawLocalImportStatusResponse,
  CatpawLocalImportLoginResponse, CatpawLocalImportApplyResponse} from '../../api/api-v1-model-routing.generated';
import {loadModelRoutingAuthority, runModelRoutingMutation, type ModelRoutingMutationResult} from './model-routing-authority';
import {prepareProviderRemoval} from './provider-mutations';
import {createCatpawConnection, type CatpawView} from './catpaw-connection';

interface CatpawPorts {
  Api: {
    oauth: {
      catpawImportStatus(): Promise<CatpawLocalImportStatusResponse | null>;
      catpawImportLogin(): Promise<CatpawLocalImportLoginResponse | null>;
      catpawImportApply(): Promise<Response | null>;
    };
    modelRouting: {deleteProvider(providerId: string, revision: number): Promise<ProviderDeleteResponse | null>};
  };
  _stgModelRouting?: ModelRoutingDocument | null;
  _renderModelRoutingTabs?: () => void;
  showConfirm(message: string, options?: {danger: boolean}): Promise<boolean>;
  showAlert(message: string): unknown;
}
const globals = () => featureRegistry as unknown as CatpawPorts;
let card: HTMLElement | null = null;

function render(view: CatpawView): void {
  if (!card?.isConnected || document.getElementById('oauthCatpawCard') !== card) return;
  if (view.busy) card.setAttribute('aria-busy', 'true');
  else card.removeAttribute('aria-busy');
  const badge = card.querySelector<HTMLElement>('#oauthCatpawStatus');
  const connect = card.querySelector<HTMLButtonElement>('#oauthCatpawImportBtn');
  const disconnect = card.querySelector<HTMLButtonElement>('#oauthCatpawDisconnectBtn');
  const hint = card.querySelector<HTMLElement>('#oauthCatpawHint');
  const {status} = view;
  if (badge) {
    badge.title = '';
    badge.textContent = !status ? t('settings.oauthCatpawChecking') : status.connected
      ? t('settings.oauthModelsReady', {n: status.model_count || 0})
      : t(status.available ? 'settings.oauthCatpawReady' : 'settings.oauthCatpawUnavailable');
    badge.className = 'oauth-status-badge' + (status?.connected ? ' authenticated' : status?.available ? ' pending' : '');
  }
  if (connect) {
    connect.style.display = status?.connected ? 'none' : '';
    connect.disabled = view.busy;
    connect.textContent = t(view.busy ? 'settings.oauthPreparing' : status?.available
      ? 'settings.oauthCatpawImport' : 'settings.oauthCatpawLogin');
  }
  if (disconnect) { disconnect.style.display = status?.connected ? '' : 'none'; disconnect.disabled = view.busy; }
  if (hint) {
    hint.style.display = view.waiting || (status && !status.connected && !status.available) ? '' : 'none';
    hint.textContent = t(view.waiting ? 'settings.oauthCatpawLoginWaiting' : 'settings.oauthCatpawHint');
  }
}

async function ensureAuthority(): Promise<void> {
  if (!globals()._stgModelRouting) await loadModelRoutingAuthority(true);
  if (!globals()._stgModelRouting) throw new Error(t('settings.providerSelectionChanged'));
}

function refreshProviderView(result: ModelRoutingMutationResult): ModelRoutingMutationResult {
  if (!result.refreshed) return result;
  try { globals()._renderModelRoutingTabs?.(); return result; }
  catch (error) { console.warn('[Settings] Catpaw Provider view refresh failed', error); return {refreshed: false}; }
}

async function importCatpaw(): Promise<ModelRoutingMutationResult & {status: {available: true; connected: true; model_count: number}}> {
  await ensureAuthority();
  let receipt: CatpawLocalImportApplyResponse | null = null;
  const result = await runModelRoutingMutation(async () => {
    const response = await globals().Api.oauth.catpawImportApply();
    const value = await response?.json() as CatpawLocalImportApplyResponse | null;
    if (!value?.connected || !value.provider_id) throw new Error(t('settings.oauthCatpawImportFailed'));
    receipt = value;
    return {revision: value.revision};
  });
  return {...refreshProviderView(result), status: {available: true, connected: true,
    model_count: (receipt as CatpawLocalImportApplyResponse | null)?.model_count || 0}};
}

const connection = createCatpawConnection({
  scope() {
    const scope = createLifecycleScope();
    const element = document.getElementById('oauthCatpawCard');
    card = element;
    const observer = new MutationObserver(() => {
      if (element && (!element.isConnected || document.getElementById(element.id) !== element)) connection.close();
    });
    observer.observe(document.body, {childList: true, subtree: true});
    scope.add(() => { observer.disconnect(); element?.removeAttribute('aria-busy'); if (card === element) card = null; });
    return scope;
  },
  async status() {
    const status = await globals().Api.oauth.catpawImportStatus();
    if (!status || typeof status.available !== 'boolean' || typeof status.connected !== 'boolean') {
      throw new Error('Catpaw status is unavailable');
    }
    return status;
  },
  async login() {
    const login = await globals().Api.oauth.catpawImportLogin();
    if (!login || typeof login.login_url !== 'string') throw new Error(t('settings.oauthCatpawImportFailed'));
    return login;
  },
  import: importCatpaw,
  async prepareDisconnect() {
    await ensureAuthority();
    const provider = globals()._stgModelRouting?.providers.find(row => row.brand === 'catpaw');
    if (!provider) throw new Error(t('settings.providerSelectionChanged'));
    const remove = prepareProviderRemoval({
      document: () => globals()._stgModelRouting || null, mutate: runModelRoutingMutation,
      remove: (id, revision) => globals().Api.modelRouting.deleteProvider(id, revision),
      selectionChanged: () => new Error(t('settings.providerSelectionChanged')),
    }, provider.provider_id);
    return async () => refreshProviderView(await remove());
  },
  confirmDisconnect: () => globals().showConfirm(t('settings.oauthCatpawDisconnectConfirm'), {danger: true}),
  openLogin(url) { window.open(url, '_blank', 'noopener'); },
  render,
  failed(operation, error) {
    globals().showAlert(operation === 'disconnect'
      ? t('settings.oauthLogoutFailed', {error: error instanceof Error ? error.message : String(error)})
      : t('settings.oauthCatpawImportFailed'));
  },
  savedWithoutRefresh() { globals().showAlert(t('settings.providerSavedDraftKept')); },
});

export const catpawSettings = {
  open: () => document.getElementById('oauthCatpawCard') ? connection.open() : Promise.resolve(),
  close: () => connection.close(),
  refresh: () => connection.refresh(),
  connect: () => connection.connect(),
  disconnect: () => connection.disconnect(),
  providerRemoval: <T>(remove: () => Promise<T>) => connection.providerRemoval(remove),
};
featureRegistry._openCatpawSettings = catpawSettings.open;
featureRegistry._destroyCatpawSettings = catpawSettings.close;
