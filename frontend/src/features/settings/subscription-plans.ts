/** Settings → Subscription sign-in plan-key cards (domestic coding plans).
 * Entry: subscriptionPlans.refresh/close. One card per provider template of
 * category 'subscription' — the template is the only roster and endpoint
 * source. Connect hands the compiled template and the pasted key to the
 * common Provider add command (the same stage the 服务商 template picker
 * uses); disconnect is the ordinary Provider removal. Status projects the
 * committed routing authority; no plan list or credential lives here.
 */
import {featureRegistry} from '../../feature-registry';
import {t} from '../../i18n';
import {brandIconHtml} from '../../core/model-brand-icons';
import {createButton} from '../../ui/button';
import type {ModelRoutingDocument, ProviderDeleteResponse} from '../../api/api-v1-model-routing.generated';
import {loadModelRoutingAuthority, runModelRoutingMutation, type ModelRoutingMutationResult} from './model-routing-authority';
import {prepareProviderRemoval} from './provider-mutations';

interface PlanTemplate {
  key: string;
  name?: string;
  brand?: string;
  category?: string;
  catalog_source?: {url?: string};
  subscription?: {key_console_url?: string};
}
/** The retained Provider view's add command, passed by the OAuth bridge. */
export interface SubscriptionPlanPorts {
  stage(bundle: unknown, apiKey: string): Promise<boolean>;
}
interface Globals {
  Api: {
    providers: {
      templates(): Promise<unknown>;
      compileTemplate(key: string): Promise<{provider_bundle?: unknown} | null>;
    };
    modelRouting: {deleteProvider(providerId: string, revision: number): Promise<ProviderDeleteResponse | null>};
  };
  _stgModelRouting?: ModelRoutingDocument | null;
  _renderModelRoutingTabs?: () => void;
  showConfirm(message: string, options?: {danger: boolean}): Promise<boolean>;
  showAlert(message: string): unknown;
}
const globals = () => featureRegistry as unknown as Globals;

let generation = 0;
let ports: SubscriptionPlanPorts | null = null;
// One plan command at a time; a reopened panel starts a new generation.
let pending = false;

function container(): HTMLElement | null { return document.getElementById('oauthPlanKeyCards'); }

function connectedModels(key: string): number | null {
  const routing = globals()._stgModelRouting;
  if (!routing?.providers.some(row => row.provider_id === key)) return null;
  const accesses = new Set(routing.provider_accesses.filter(row => row.provider_id === key)
    .map(row => row.provider_access_id));
  return routing.offerings.filter(row => accesses.has(row.provider_access_id)).length;
}

function link(href: string | undefined, label: string): HTMLAnchorElement | null {
  if (!href?.startsWith('https://')) return null;
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  anchor.textContent = label;
  return anchor;
}

function card(template: PlanTemplate): HTMLElement {
  const models = connectedModels(template.key);
  const root = document.createElement('div');
  root.className = 'oauth-provider-card';
  root.dataset.planKey = template.key;
  const header = document.createElement('div');
  header.className = 'oauth-provider-header';
  const icon = document.createElement('span');
  icon.innerHTML = brandIconHtml(template.brand || 'generic', 20);
  const name = document.createElement('span');
  name.className = 'oauth-provider-name';
  name.textContent = template.name || template.key;
  const badge = document.createElement('span');
  badge.className = 'oauth-status-badge' + (models === null ? '' : ' authenticated');
  badge.textContent = models === null ? t('settings.oauthNotLoggedIn')
    : t('settings.oauthModelsReady', {n: models});
  const actions = document.createElement('div');
  actions.className = 'oauth-provider-actions';
  header.append(icon, name, badge, actions);

  const links = document.createElement('p');
  links.className = 'oauth-provider-desc';
  for (const anchor of [link(template.subscription?.key_console_url, t('settings.subscriptionPlanGetKey')),
    link(template.catalog_source?.url, t('settings.subscriptionPlanTerms'))]) {
    if (anchor) links.append(anchor, document.createTextNode(' '));
  }
  root.append(header, links);

  if (models === null) {
    const row = document.createElement('div');
    row.className = 'oauth-plan-key-row';
    const input = document.createElement('input');
    input.type = 'password';
    input.className = 'ui-input';
    input.autocomplete = 'off';
    input.placeholder = t('settings.subscriptionPlanKeyPlaceholder');
    input.setAttribute('aria-label', `${template.name || template.key} — ${t('settings.subscriptionPlanKeyPlaceholder')}`);
    const button = createButton({label: t('settings.subscriptionPlanConnect'), variant: 'primary', size: 'sm',
      onClick: () => { void connect(template, input, button); }});
    row.append(input, button);
    root.append(row);
  } else {
    const button = createButton({label: t('settings.oauthDisconnect'), variant: 'danger', size: 'sm',
      onClick: () => { void disconnect(template, button); }});
    actions.append(button);
  }
  return root;
}

let templates: PlanTemplate[] = [];

function render(): void {
  const host = container();
  if (!host) return;
  host.replaceChildren(...templates.map(card));
}

async function refresh(next: SubscriptionPlanPorts): Promise<void> {
  ports = next;
  const epoch = ++generation;
  if (!container()) return;
  try {
    const response = await globals().Api.providers.templates();
    const rows = (Array.isArray(response) ? response : (response as {items?: unknown})?.items) as PlanTemplate[] | undefined;
    if (!globals()._stgModelRouting) await loadModelRoutingAuthority(true);
    if (epoch !== generation) return;
    templates = (rows || []).filter(row => row.category === 'subscription');
    render();
  } catch (error) {
    console.warn('[Settings] subscription plan cards unavailable', error);
  }
}

function settled(result: ModelRoutingMutationResult): void {
  if (result.refreshed) {
    try { globals()._renderModelRoutingTabs?.(); } catch (error) { console.warn('[Settings] Provider view refresh failed', error); }
  } else {
    globals().showAlert(t('settings.providerSavedDraftKept'));
  }
}

async function connect(template: PlanTemplate, input: HTMLInputElement, button: HTMLButtonElement): Promise<void> {
  const apiKey = input.value.trim();
  if (!apiKey) { globals().showAlert(t('settings.subscriptionPlanKeyMissing')); return; }
  const stage = ports?.stage;
  if (pending || !stage) return;
  const epoch = generation;
  pending = true;
  button.disabled = true;
  let staged = false;
  try {
    const compiled = await globals().Api.providers.compileTemplate(template.key);
    if (!compiled?.provider_bundle) throw new Error(template.key);
    staged = await stage(compiled.provider_bundle, apiKey);
  } catch (error) {
    globals().showAlert(t('settings.subscriptionPlanConnectFailed', {
      error: error instanceof Error ? error.message : String(error)}));
  } finally {
    pending = false;
    button.disabled = false;
    if (staged) input.value = '';
    if (staged && epoch === generation) render();
  }
}

async function disconnect(template: PlanTemplate, button: HTMLButtonElement): Promise<void> {
  if (pending) return;
  const epoch = generation;
  pending = true;
  button.disabled = true;
  try {
    if (!globals()._stgModelRouting) await loadModelRoutingAuthority(true);
    const remove = prepareProviderRemoval({
      document: () => globals()._stgModelRouting || null, mutate: runModelRoutingMutation,
      remove: (id, revision) => globals().Api.modelRouting.deleteProvider(id, revision),
      selectionChanged: () => new Error(t('settings.providerSelectionChanged')),
    }, template.key);
    if (!await globals().showConfirm(t('settings.subscriptionPlanDisconnectConfirm',
      {name: template.name || template.key}), {danger: true})) return;
    settled(await remove());
  } catch (error) {
    globals().showAlert(t('settings.oauthLogoutFailed', {
      error: error instanceof Error ? error.message : String(error)}));
  } finally {
    pending = false;
    button.disabled = false;
    if (epoch === generation) render();
  }
}

function close(): void {
  generation++;
  ports = null;
  templates = [];
  container()?.replaceChildren();
}

export const subscriptionPlans = {refresh, close};
featureRegistry._destroySubscriptionPlans = subscriptionPlans.close;
