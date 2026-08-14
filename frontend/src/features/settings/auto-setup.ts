import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

interface ProbeModel {
  catalog_managed?: boolean;
  catalog_source?: string;
  [key: string]: unknown;
}

interface ProbeSummary {
  text?: number;
  thinking?: number;
  vision?: number;
  cheap?: number;
  image_gen?: number;
  embedding?: number;
}

interface ProbeResponse {
  ok?: boolean;
  error?: string;
  models?: ProbeModel[];
  summary?: ProbeSummary;
  balance_url?: string;
  thinking_format?: string;
  brand?: string;
  name?: string;
}

interface ProviderConfig {
  id: string;
  name: string;
  base_url: string;
  api_keys: string[];
  enabled: boolean;
  models: ProbeModel[];
  brand: string;
  balance_url: string;
  model_catalog_sync: { mode: 'auto' };
  thinking_format?: string;
}

interface ProvidersApi {
  probe(
    baseUrl: string,
    apiKey: string,
    modelsPath: string,
  ): Promise<ProbeResponse | null>;
}

type AutoSetupWindow = Window & {
  Api?: { providers?: ProvidersApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  Icon?: (name: string, size?: number) => string;
  escapeHtml?: (value: unknown) => string;
  _showAutoSetupModal?: () => void;
  _runAutoProbe?: () => void;
  _showAutoStatus?: (type: string, message: string) => void;
  _destroyAutoSetup?: () => void;
  _stgProviders: ProviderConfig[];
  _serverConfig?: unknown;
  _renderProvidersTab?: () => void;
  _renderPresetsTab?: (config: unknown) => void;
};

let modalScope: LifecycleScope | null = null;
let modalGeneration = 0;

function globals(): AutoSetupWindow {
  return featureRegistry as unknown as AutoSetupWindow;
}

function translate(key: string, values?: Record<string, unknown>): string {
  return globals().t?.(key, values) || key;
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (helper) return helper(value);
  const node = document.createElement('span');
  node.textContent = String(value ?? '');
  return node.innerHTML;
}

function input(id: string): HTMLInputElement | null {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement ? element : null;
}

function probeButton(): HTMLButtonElement | null {
  const element = document.getElementById('stgAutoProbeBtn');
  return element instanceof HTMLButtonElement ? element : null;
}

function providersApi(): ProvidersApi {
  const api = globals().Api?.providers;
  if (!api) throw new Error('Provider API is not ready');
  return api;
}

export function destroyAutoSetup(): void {
  modalGeneration += 1;
  modalScope?.destroy();
  modalScope = null;
  document.getElementById('stgAutoSetupModal')?.remove();
}

export function showAutoStatus(type: string, message: string): void {
  const status = document.getElementById('stgAutoStatus');
  if (!(status instanceof HTMLElement)) return;
  status.style.display = 'block';
  status.className = `stg-auto-status stg-auto-${type}`;
  status.textContent = message;
}

export function showAutoSetupModal(): void {
  destroyAutoSetup();
  const generation = modalGeneration;
  const scope = createLifecycleScope();
  modalScope = scope;
  const searchIcon = globals().Icon?.('search', 13) || '';
  const markup = `
    <div id="stgAutoSetupModal" class="stg-modal-overlay">
      <div class="stg-modal">
        <div class="stg-modal-header">
          <span class="stg-modal-title">${escape(translate('settings.asTitle'))}</span>
          <button type="button" class="stg-modal-close" data-auto-close>✕</button>
        </div>
        <div class="stg-modal-body">
          <p class="stg-modal-desc">${escape(translate('settings.asDesc'))}</p>
          <div class="stg-field">
            <label>${escape(translate('settings.asUrlLabel'))} <span class="stg-required">*</span></label>
            <input type="text" id="stgAutoUrl" placeholder="https://api.deepseek.com" autocomplete="url">
            <span class="stg-hint">${escape(translate('settings.asUrlHint'))}</span>
          </div>
          <div class="stg-field">
            <label>${escape(translate('settings.asKeyLabel'))} <span class="stg-required">*</span></label>
            <input type="password" id="stgAutoKey" placeholder="sk-..." autocomplete="off">
          </div>
          <div class="stg-field">
            <label>${escape(translate('settings.asModelsPathLabel'))} <span class="stg-hint">${escape(translate('settings.asModelsPathHint'))}</span></label>
            <input type="text" id="stgAutoModelsPath" placeholder="/models">
          </div>
          <div id="stgAutoStatus" class="stg-auto-status" style="display:none"></div>
        </div>
        <div class="stg-modal-footer">
          <button type="button" class="stg-btn-secondary" data-auto-close>${escape(translate('settings.cancel'))}</button>
          <button type="button" class="stg-btn-primary" id="stgAutoProbeBtn">${searchIcon} ${escape(translate('settings.asProbeBtn'))}</button>
        </div>
      </div>
    </div>`;
  document.body.insertAdjacentHTML('beforeend', markup);

  const overlay = document.getElementById('stgAutoSetupModal');
  if (!(overlay instanceof HTMLElement)) {
    destroyAutoSetup();
    return;
  }
  scope.listen(overlay, 'click', (event) => {
    if (event.target === overlay) destroyAutoSetup();
  });
  for (const close of overlay.querySelectorAll('[data-auto-close]')) {
    scope.listen(close, 'click', destroyAutoSetup);
  }
  const button = probeButton();
  if (button) scope.listen(button, 'click', () => { void runAutoProbe(); });
  scope.timeout(() => {
    if (generation === modalGeneration) input('stgAutoUrl')?.focus();
  }, 100);
}

function pushSummary(
  parts: string[],
  summary: ProbeSummary,
  field: keyof ProbeSummary,
  key: string,
): void {
  const count = summary[field];
  if (count) parts.push(translate(key, { n: count }));
}

function addProvider(response: ProbeResponse, baseUrl: string, apiKey: string): void {
  const models = response.models ?? [];
  for (const model of models) {
    model.catalog_managed = true;
    model.catalog_source = 'provider';
  }
  const provider: ProviderConfig = {
    id: `${response.brand || 'prov'}_${Date.now().toString(36)}`,
    name: response.name || 'Auto Provider',
    base_url: baseUrl,
    api_keys: [apiKey],
    enabled: true,
    models,
    brand: response.brand || 'generic',
    balance_url: response.balance_url || '',
    model_catalog_sync: { mode: 'auto' },
  };
  if (response.thinking_format) provider.thinking_format = response.thinking_format;
  const shared = globals();
  shared._stgProviders.unshift(provider);
  shared._renderProvidersTab?.();
  shared._renderPresetsTab?.(shared._serverConfig);
}

export async function runAutoProbe(): Promise<void> {
  const generation = modalGeneration;
  const scope = modalScope;
  const urlInput = input('stgAutoUrl');
  const keyInput = input('stgAutoKey');
  const pathInput = input('stgAutoModelsPath');
  const button = probeButton();
  let baseUrl = urlInput?.value.trim() || '';
  const apiKey = keyInput?.value.trim() || '';
  const modelsPath = pathInput?.value.trim() || '';

  if (!baseUrl) {
    showAutoStatus('error', translate('settings.fillUrl'));
    return;
  }
  if (!apiKey) {
    showAutoStatus('error', translate('settings.fillKey'));
    return;
  }
  if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
    baseUrl = `https://${baseUrl}`;
    if (urlInput) urlInput.value = baseUrl;
  }

  if (button) {
    button.disabled = true;
    button.textContent = translate('settings.asProbing');
  }
  showAutoStatus('loading', translate('settings.discoveringModels'));

  try {
    const response = await providersApi().probe(baseUrl, apiKey, modelsPath);
    if (generation !== modalGeneration || modalScope !== scope) return;
    if (!response) {
      showAutoStatus('error', translate('settings.asProbeNetFail'));
      return;
    }
    if (!response.ok) {
      showAutoStatus('error', response.error || translate('settings.probeFailed'));
      return;
    }

    const models = response.models ?? [];
    const summary = response.summary ?? {};
    const parts: string[] = [];
    pushSummary(parts, summary, 'text', 'settings.asTextModels');
    pushSummary(parts, summary, 'thinking', 'settings.asThinkingModels');
    pushSummary(parts, summary, 'vision', 'settings.asVisionModels');
    pushSummary(parts, summary, 'cheap', 'settings.asCheapModels');
    pushSummary(parts, summary, 'image_gen', 'settings.asIgModels');
    pushSummary(parts, summary, 'embedding', 'settings.asEmbeddingModels');
    const modelSummary = parts.join(translate('settings.asModelsJoin'))
      || translate('settings.asModelsCount', { n: models.length });
    showAutoStatus('success', translate('settings.asDiscovered', {
      n: models.length,
      summary: modelSummary,
      balance: response.balance_url
        ? translate('settings.asBalanceDetected')
        : '',
      thinking: response.thinking_format
        ? translate('settings.asThinkingFormat', { fmt: response.thinking_format })
        : '',
    }));
    addProvider(response, baseUrl, apiKey);
    scope?.timeout(() => {
      if (generation !== modalGeneration) return;
      destroyAutoSetup();
      const first = document.querySelector('.stg-provider-card');
      if (first instanceof HTMLElement) {
        first.classList.add('expanded');
        first.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }, 1500);
  } catch (error: unknown) {
    if (generation !== modalGeneration || modalScope !== scope) return;
    const message = error instanceof Error ? error.message : String(error);
    showAutoStatus(
      'error', translate('settings.asNetworkError', { error: message }));
  } finally {
    if (generation === modalGeneration && modalScope === scope && button) {
      button.disabled = false;
      button.textContent = translate('settings.asProbeBtn');
    }
  }
}

const bridge = globals();
bridge._showAutoSetupModal = showAutoSetupModal;
bridge._runAutoProbe = () => { void runAutoProbe(); };
bridge._showAutoStatus = showAutoStatus;
bridge._destroyAutoSetup = destroyAutoSetup;
