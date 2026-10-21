import '../styles/settings/settings.generated.css';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './settings/devices.css';
import './settings/tools-inventory.css';
import './settings/settings-comfort.css';
import { setRuntimeService } from '../runtime/app-runtime.js';
import { featureRegistry } from '../feature-registry';
import { modelPricePresentation } from './settings/model-price-localization';

import { manualMatrixProbe } from './settings/manual-matrix-probe';
import { jumpToSetting } from './settings/setting-jump';

import { mountRequestNameEditors, planRequestNameFaces, suggestedRequestNames } from './settings/provider-request-names';
import { openRequestNamePanel, requestNameDraftFrom } from './settings/provider-request-name-panel';
import { createProviderTemplatePicker } from './settings/provider-template-picker';
import { mountProviderReasoningEditors } from './settings/reasoning-editor';
import { brandIconHtml } from '../core/model-brand-icons';
import type { CompiledProviderBundle, ProviderProbeResponse, ProviderTemplateCompileResponse } from '../api/api-v1-model-routing.generated';
import { t } from '../i18n';
import type { I18nKey } from '../i18n';
import './settings/section-requires';
import './settings/private-hosts';
import './settings/site-packs';
import './settings/browser-access';
import './settings/devices';
import './settings/speech';
import './settings/credentials-vault';
import './settings/auth-sources';
import './settings/model-catalog-tab';
import './settings/model-routing-sync';
import './settings/system-prompt-editor';
import './settings/visibility-defaults';
import './settings/save-export';
import './settings/local-deploy';
import './settings/database';
import './settings/core-panel';
import './settings/tools-inventory';
import '../runtime/settings-presenters.generated.js';

const settingsCompatibility = featureRegistry;
setRuntimeService('modelPricePresentation', modelPricePresentation);

setRuntimeService('manualMatrixProbe', manualMatrixProbe);
setRuntimeService('jumpToSetting', jumpToSetting);
setRuntimeService('mountProviderReasoningEditors', mountProviderReasoningEditors);
setRuntimeService('mountRequestNameEditors', mountRequestNameEditors);
// Request-name panel: edits the wire id AND the headers that select its
// route. A plain prompt cannot express a per-request-name selector header,
// which is what a gateway needing x-context-provider requires.
setRuntimeService('openRequestNamePanel', openRequestNamePanel);
setRuntimeService('requestNameDraftFrom', requestNameDraftFrom);
setRuntimeService('planProviderRequestNames', async (models: Array<{model_id: string}>, urls: string[]) => {
  const response = await providerTemplateApi().templates();
  const templates = (Array.isArray(response) ? response : (response as {items?: unknown})?.items) as import('./settings/provider-template-picker').ProviderTemplate[] | undefined;
  return Object.fromEntries(models.map(model => [model.model_id, suggestedRequestNames(model.model_id, urls, templates ?? [])]));
});
// Which Connection each request name must be sent over. A wire id pinned by a
// template face (aigc kunlun) answers only on that face, so this is what keeps
// a newly added request name routable instead of merely present in the matrix.
setRuntimeService('planProviderRequestNameFaces', async (names: string[], urls: string[]) => {
  const response = await providerTemplateApi().templates();
  const templates = (Array.isArray(response) ? response : (response as {items?: unknown})?.items) as import('./settings/provider-template-picker').ProviderTemplate[] | undefined;
  return planRequestNameFaces(names, urls, templates ?? []);
});

let selectProviderTemplate: (key: string) => void = () => undefined;
let stageProviderTemplate: (bundle: CompiledProviderBundle, key: string) => Promise<boolean> = async () => false;
const providerTemplateApi = () => (featureRegistry.Api as { providers: {
  templates(): Promise<unknown>;
  compileTemplate(key: string): Promise<ProviderTemplateCompileResponse>;
  probe(baseUrl: string, apiKey: string, path: string): Promise<ProviderProbeResponse>;
} }).providers;
const providerTemplatePicker = createProviderTemplatePicker({
  document,
  translate: (key) => t(key as I18nKey, {} as never),
  load: () => providerTemplateApi().templates(),
  select: (key) => { selectProviderTemplate(key); },
  iconHtml: (brand) => brandIconHtml(brand, 22),
  discover: async (key, apiKey, current) => {
    const compiled = (await providerTemplateApi().compileTemplate(key)).provider_bundle;
    if (!current()) return;
    const result = await providerTemplateApi().probe(compiled.connections[0].base_url, apiKey, '');
    if (!current()) return;
    if (!result?.provider_bundle || !result.model_count) throw new Error('Discovery unavailable');
    const bundle = result.provider_bundle;
    // Preserve the chosen operator's presentation, not an inferred model vendor.
    bundle.provider.name = compiled.provider.name;
    bundle.provider.brand = compiled.provider.brand;
    bundle.provider_access.display_name = compiled.provider_access.display_name;
    if (!await stageProviderTemplate(bundle, apiKey)) throw new Error('Provider not saved');
  },
});
setRuntimeService('_toggleProviderTemplatePicker', (trigger: HTMLElement, select: (key: string) => void,
  stage: typeof stageProviderTemplate) => {
  selectProviderTemplate = select;
  stageProviderTemplate = stage;
  providerTemplatePicker.toggle(trigger);
});
setRuntimeService('_destroyProviderTemplatePicker', providerTemplatePicker.destroy);

// Creator/Model is a lazy, typed, read-only Settings owner. The retained
// adapter passes only the v2 document; the feature's public type intentionally
// omits every Provider/Offering/Deployment field.
let modelCatalogPanel: Promise<typeof import('./model-catalog/panel')> | null = null;

function ensureModelCatalogPanel() {
  if (!modelCatalogPanel) modelCatalogPanel = import('./model-catalog/panel');
  return modelCatalogPanel;
}

function renderModelCatalogPanel(documentValue: unknown,
  stageReasoning?: Parameters<typeof import('./model-catalog/panel').renderModelCatalogPanel>[1]): void {
  void ensureModelCatalogPanel().then((owner) => {
    owner.renderModelCatalogPanel(documentValue as Parameters<typeof owner.renderModelCatalogPanel>[0], stageReasoning);
  });
}

function setModelCatalogSearch(value: unknown): void {
  void ensureModelCatalogPanel().then((owner) => owner.setModelCatalogSearch(value));
}

function destroyModelCatalogPanel(): void {
  if (!modelCatalogPanel) return;
  void modelCatalogPanel.then((owner) => owner.destroyModelCatalogPanel());
}

/* Live-sync hook: drop the panel's cached AA block so the repaint after a
 * model_routing.changed hint refetches scores once. No-op until the panel
 * chunk has loaded. */
function invalidateModelCatalogAaScores(): void {
  if (!modelCatalogPanel) return;
  void modelCatalogPanel.then((owner) => owner.invalidateModelCatalogAaScores());
}

setRuntimeService('_renderModelCatalogPanel', renderModelCatalogPanel);
setRuntimeService('_setModelCatalogSearchOwner', setModelCatalogSearch);
setRuntimeService('_destroyModelCatalogPanel', destroyModelCatalogPanel);
setRuntimeService('_invalidateModelCatalogAaScores', invalidateModelCatalogAaScores);

for (const name of [
  '_destroyPrivateHosts', '_destroyBrowserAccess', '_destroyDevicesTab',
  '_destroySpeechTab',
  '_destroyCredentialsVault', '_destroyAuthSources',
] as const) {
  const service = settingsCompatibility[name];
  if (typeof service === 'function') setRuntimeService(name, service);
}

window.addEventListener('tofu:language-change', () => {
  const modal = document.getElementById('settingsModal');
  if (!modal?.classList.contains('open')) return;
  for (const name of ['_renderMcpCatalog', '_renderProvidersTab'] as const) {
    const repaint = settingsCompatibility[name];
    if (typeof repaint === 'function') {
      try { repaint(); } catch { /* panel state may not be initialized yet */ }
    }
  }
  if (modelCatalogPanel) {
    void modelCatalogPanel.then((owner) => owner.repaintModelCatalogPanel());
  }
});

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('settings', name, args, stub);
}
