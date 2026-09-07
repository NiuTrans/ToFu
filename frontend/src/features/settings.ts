import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './settings/devices.css';
import './settings/tools-inventory.css';
import './settings/settings-comfort.css';
import { setRuntimeService } from '../runtime/app-runtime.js';
import { featureRegistry } from '../feature-registry';
import { modelPricePresentation } from './settings/model-price-localization';
import './settings/section-requires';
import './settings/private-hosts';
import './settings/browser-access';
import './settings/devices';
import './settings/speech';
import './settings/credentials-vault';
import './settings/auth-sources';
import './settings/tools-inventory';
import '../runtime/settings-presenters.generated.js';

const settingsCompatibility = featureRegistry;
setRuntimeService('modelPricePresentation', modelPricePresentation);

// Creator/Model is a lazy, typed, read-only Settings owner. The retained
// adapter passes only the v2 document; the feature's public type intentionally
// omits every Provider/Offering/Deployment field.
let modelCatalogPanel: Promise<typeof import('./model-catalog/panel')> | null = null;

function ensureModelCatalogPanel() {
  if (!modelCatalogPanel) modelCatalogPanel = import('./model-catalog/panel');
  return modelCatalogPanel;
}

function renderModelCatalogPanel(documentValue: unknown): void {
  void ensureModelCatalogPanel().then((owner) => {
    owner.renderModelCatalogPanel(documentValue as Parameters<typeof owner.renderModelCatalogPanel>[0]);
  });
}

function setModelCatalogSearch(value: unknown): void {
  void ensureModelCatalogPanel().then((owner) => owner.setModelCatalogSearch(value));
}

function destroyModelCatalogPanel(): void {
  if (!modelCatalogPanel) return;
  void modelCatalogPanel.then((owner) => owner.destroyModelCatalogPanel());
}

setRuntimeService('_renderModelCatalogPanel', renderModelCatalogPanel);
setRuntimeService('_setModelCatalogSearchOwner', setModelCatalogSearch);
setRuntimeService('_destroyModelCatalogPanel', destroyModelCatalogPanel);

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
