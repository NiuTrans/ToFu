import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './settings/provider-surfaces.css';
import { setRuntimeService } from '../runtime/app-runtime.js';
import { featureRegistry } from '../feature-registry';
import { modelPricePresentation } from './settings/model-price-localization';
import './settings/section-requires';
import './settings/private-hosts';
import './settings/browser-access';
import './settings/devices';
import './settings/speech';
import './settings/credentials-vault';
import './settings/auto-setup';
import './settings/balance';
import './settings/auth-sources';
import './settings/key-stats';
import './settings/tools-inventory';

const settingsCompatibility = featureRegistry;
setRuntimeService('modelPricePresentation', modelPricePresentation);

// The centralized Models panel is a lazy chunk: retained Settings reaches it
// through this bridge and the panel publishes its real seams once evaluated.
let modelCatalogPanel: Promise<typeof import('./model-catalog/panel')> | null = null;

function ensureModelCatalogPanel() {
  if (!modelCatalogPanel) {
    modelCatalogPanel = import('./model-catalog/panel');
  }
  return modelCatalogPanel;
}

function renderModelCatalogPanel(): void {
  void ensureModelCatalogPanel().then((owner) => owner.renderModelCatalogPanel());
}

function destroyModelCatalogPanel(): void {
  void ensureModelCatalogPanel().then((owner) => owner.destroyModelCatalogPanel());
}

setRuntimeService('_renderModelCatalogPanel', renderModelCatalogPanel);
setRuntimeService('_destroyModelCatalogPanel', destroyModelCatalogPanel);
for (const name of [
  '_startBalancePolling', '_stopBalancePolling', '_loadKeyStats',
  '_startModelHealthPolling', '_stopModelHealthPolling',
  '_destroyPrivateHosts', '_destroyBrowserAccess', '_destroyDevicesTab',
  '_destroySpeechTab',
  '_destroyCredentialsVault', '_destroyAutoSetup', '_destroyAuthSources',
  '_destroyKeyStats',
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
    void modelCatalogPanel.then((owner) => {
      try { owner.repaintModelCatalogPanel(); } catch { /* not mounted yet */ }
    });
  }
});

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('settings', name, args, stub);
}
