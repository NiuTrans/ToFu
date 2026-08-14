import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { setRuntimeService } from '../runtime/app-runtime.js';
import { featureRegistry } from '../feature-registry';
import './settings/section-requires';
import './settings/private-hosts';
import './settings/devices';
import './settings/speech';
import './settings/credentials-vault';
import './settings/auto-setup';
import './settings/balance';
import './settings/auth-sources';
import './settings/key-stats';

const settingsCompatibility = featureRegistry;
for (const name of [
  '_startBalancePolling', '_stopBalancePolling', '_loadKeyStats',
  '_startModelHealthPolling', '_stopModelHealthPolling',
  '_destroyPrivateHosts', '_destroyDevicesTab', '_destroySpeechTab',
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
});

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('settings', name, args, stub);
}
