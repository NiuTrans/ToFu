/** Miscellaneous Settings config reads and cache adoption. Reads within one
 * opening coalesce; retirement prevents older config/pricing publication.
 * Export uses the raw reader so it never mutates an editor as a side effect.
 * Depends on the API/retained state ports and the Settings read lifetime.
 */
import {featureRegistry} from '../../feature-registry';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';

export interface ModelPriceDisplayPolicy {
  base_currency?: string;
  usd_rates?: Record<string, number>;
  updated_at?: number;
  source?: string;
}

export interface CoreServerConfig {
  presets?: Record<string, unknown>;
  model_pricing?: unknown;
  model_price_display?: ModelPriceDisplayPolicy;
  [key: string]: unknown;
}

interface ConfigRuntime {
  Api?: {serverConfig?: {get(options?: {signal?: AbortSignal}): Promise<CoreServerConfig | null>}};
  debugLog?: (text: string, level?: string) => void;
  _serverConfig?: CoreServerConfig | null;
  _setModelPricingCache?: (pricing: unknown) => void;
  _modelPriceDisplayPolicy?: ModelPriceDisplayPolicy;
}
const runtime = () => featureRegistry as unknown as ConfigRuntime;

export async function readServerConfig(signal?: AbortSignal): Promise<CoreServerConfig | null> {
  if (signal?.aborted) return null;
  try {
    runtime().debugLog?.('[Settings] Loading server config…', 'info');
    const value = await runtime().Api?.serverConfig?.get({signal});
    if (!value) throw new Error('serverConfig.get returned null');
    return value;
  } catch (error) {
    if (!signal?.aborted) runtime().debugLog?.('[Settings] Failed to load server config: ' + String(error), 'error');
    return null;
  }
}

interface ConfigRead {
  session: SettingsReadSession | null;
  promise: Promise<CoreServerConfig | null>;
}
let pending: ConfigRead | null = null;

export function loadServerConfig(session = currentSettingsReadSession()): Promise<CoreServerConfig | null> {
  if (session && !session.isCurrent()) return Promise.resolve(null);
  if (pending?.session === session) return pending.promise;
  const request: ConfigRead = {
    session,
    promise: Promise.resolve().then(async () => {
      try {
        const config = await readServerConfig(session?.scope.signal);
        if (pending !== request || session && !session.isCurrent()) return null;
        const bridge = runtime();
        bridge._serverConfig = config;
        if (config) {
          bridge.debugLog?.('[Settings] Misc server config loaded: ' + Object.keys(config.presets || {}).length + ' presets; providers load from model-routing v2', 'info');
          if (config.model_pricing) bridge._setModelPricingCache?.(config.model_pricing);
          if (config.model_price_display?.usd_rates) bridge._modelPriceDisplayPolicy = config.model_price_display;
        }
        return config;
      } catch (error) {
        if (!session || session.isCurrent()) runtime().debugLog?.('[Settings] Config publication failed: ' + String(error), 'error');
        return null;
      } finally {
        // Clear within the read, before resolving waiters (no settlement gap).
        if (pending === request) pending = null;
      }
    }),
  };
  pending = request;
  return request.promise;
}
