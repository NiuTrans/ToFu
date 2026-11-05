/** Settings form inputs. Captures detached wire payloads and feature choices
 * before writes await; local config application remains synchronous. Routing
 * documents and credential transactions stay with model-routing-authority.
 * Entry points: captureSettingsSaveInput, settingsSaveInputChanged,
 * applyLocalSettingsInput. Depends on retained collectors and search-gateway.
 */
import {featureRegistry} from '../../feature-registry';
import {collectSearchGatewayPayload, type SearchGatewayPayload} from './search-gateway';

interface SaveExportServerConfig {
  hidden_models?: string[];
  hidden_ig_models?: string[];
  [key: string]: unknown;
}

interface SaveExportSearchPayload {
  skip_domains?: unknown;
  gateway?: SearchGatewayPayload;
}

export interface ServerSettingsPayload {
  presets: Record<string, unknown>;
  search: SaveExportSearchPayload;
  hidden_models: string[];
  hidden_ig_models: string[];
  model_defaults: Record<string, string>;
  cost_experiment?: unknown;
  proxy_pool?: unknown;
  proxy_bypass_domains?: unknown;
  feishu?: unknown;
  mt_provider?: unknown;
}

interface InputPorts {
  config?: Record<string, unknown>;
  debugLog?: (message: string, level?: string) => void;
  refreshInputSendHint?: () => void;
  _configForPersist?: () => unknown;
  _collectResponsesExperimentControls?: () => void;
  _serverConfig?: SaveExportServerConfig | null;
  _stgPresets?: Record<string, unknown>;
  ChipInput?: {getValues(id: string): unknown};
  _collectModelDefaults?: () => Record<string, string>;
  _collectCostExperimentConfig?: () => unknown;
  _collectProxyPool?: () => unknown;
  _collectProxyBypassDomains?: () => unknown;
  _collectFeishuConfig?: () => unknown;
  _collectMtProviderConfig?: () => unknown;
}
const globals = () => featureRegistry as unknown as InputPorts;

const featureFields = [
  ['settingTradingEnabled', 'trading_enabled'],
  ['settingPptxTranslateEnabled', 'pptx_translate_enabled'],
  ['settingDebugMode', 'debug_mode'],
  ['settingOptimizerEnabled', 'optimizer_enabled'],
] as const;
export type SettingsFeatureFlag = typeof featureFields[number][1];
export interface SettingsSaveInput {
  flags: Array<{flag: SettingsFeatureFlag; value: boolean}>;
  server: ServerSettingsPayload | null;
}

function elementValue(id: string): string {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement
      || element instanceof HTMLSelectElement
      || element instanceof HTMLTextAreaElement
    ? element.value
    : '';
}

function elementChecked(id: string): boolean {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement ? element.checked : false;
}

function captureServerPayload(): ServerSettingsPayload {
  const bridge = globals();
  // Strip empty preset mappings — especially 'opus' should never be pinned
  // to a specific version; leaving it unset lets the code default (LLM_MODEL) apply.
  const cleanPresets: Record<string, unknown> = {};
  const stagedPresets = bridge._stgPresets || {};
  for (const k in stagedPresets) {
    if (stagedPresets[k]) cleanPresets[k] = stagedPresets[k];
  }

  const serverConfig = bridge._serverConfig;
  const payload: ServerSettingsPayload = {
    presets: cleanPresets,
    search: {},
    hidden_models: (serverConfig && serverConfig.hidden_models) || [],
    hidden_ig_models: (serverConfig && serverConfig.hidden_ig_models) || [],
    model_defaults: bridge._collectModelDefaults ? bridge._collectModelDefaults() : {},
  };
  if (typeof bridge._collectCostExperimentConfig === 'function') {
    const costExperimentCfg = bridge._collectCostExperimentConfig();
    if (costExperimentCfg !== null) payload.cost_experiment = costExperimentCfg;
  }
  // Search tab: limits are fixed backend defaults; only the blocklist and
  // the optional gateway are settings.
  if (typeof bridge.ChipInput !== 'undefined') payload.search.skip_domains = bridge.ChipInput?.getValues('settingSkipDomains');
  // Omitted when the 搜索 API card is not in use; the server keeps the saved gateway.
  const searchGateway = collectSearchGatewayPayload();
  if (searchGateway) payload.search.gateway = searchGateway;

  // Network — proxy pool (ordered, scoped). The pool editor owns
  // proxying: the backend retires the legacy single-proxy slot whenever
  // the key is present. Container absent (legacy/other surfaces) → leave
  // the server's proxy config untouched.
  const pool = (typeof bridge._collectProxyPool === 'function') ? bridge._collectProxyPool() : null;
  if (pool !== null) {
    payload.proxy_pool = pool;
  }

  // Network — directly editable bypass rows (feeds proxies_for + no_proxy).
  const bypass = (typeof bridge._collectProxyBypassDomains === 'function')
    ? bridge._collectProxyBypassDomains() : null;
  if (bypass !== null) payload.proxy_bypass_domains = bypass;

  // Feishu bot config
  if (typeof bridge._collectFeishuConfig === 'function') {
    payload.feishu = bridge._collectFeishuConfig();
  }

  // Machine translation provider config
  if (typeof bridge._collectMtProviderConfig === 'function') {
    payload.mt_provider = bridge._collectMtProviderConfig();
  }

  // Detach every collector result exactly as the JSON transport would.
  return JSON.parse(JSON.stringify(payload)) as ServerSettingsPayload;
}

export function captureSettingsSaveInput(): SettingsSaveInput {
  const flags = featureFields.filter(([id]) => document.getElementById(id))
    .map(([id, flag]) => ({flag, value: elementChecked(id)}));
  return {flags, server: globals()._serverConfig ? captureServerPayload() : null};
}

export function settingsSaveInputChanged(input: SettingsSaveInput): boolean {
  try { return JSON.stringify(captureSettingsSaveInput()) !== JSON.stringify(input); }
  catch { return true; } // A newer incomplete form must remain editable.
}

export function applyLocalSettingsInput(): void {
  const bridge = globals();
  const config = bridge.config as Record<string, unknown>;
  // 1. Client-side config (General tab)
  if (typeof bridge._collectResponsesExperimentControls === 'function') {
    bridge._collectResponsesExperimentControls();
  }
  config.systemPrompt = elementValue('settingSystem');
  if (document.getElementById('settingSystemPromptMode')) {
    config.systemPromptMode = (elementValue('settingSystemPromptMode') === 'replace') ? 'replace' : 'append';
  }
  if (document.getElementById('settingSystemDisabledBlocks')) {
    let disabledIds: unknown = [];
    try { disabledIds = JSON.parse(elementValue('settingSystemDisabledBlocks') || '[]'); } catch { disabledIds = []; }
    if (!Array.isArray(disabledIds)) disabledIds = [];
    config.systemPromptBlocks = { disabled: disabledIds };
  }
  if (document.getElementById('settingDefaultThinkingDepth')) {
    const oldDefault = config.defaultThinkingDepth;
    config.defaultThinkingDepth = elementValue('settingDefaultThinkingDepth') || 'off';
    // Propagate: if current depth was the old default, update it to the new default
    if (config.thinkingDepth === oldDefault) {
      config.thinkingDepth = config.defaultThinkingDepth;
    }
  }
  // Auto-generate conversation title toggle
  if (document.getElementById('settingAutoGenerateTitle')) {
    config.autoGenerateTitle = elementChecked('settingAutoGenerateTitle');
  }

  // Input send mode
  if (document.getElementById('settingInputSendMode')) {
    config.inputSendMode = (elementValue('settingInputSendMode') === 'ctrl_enter') ? 'ctrl_enter' : 'enter';
    if (typeof bridge.refreshInputSendHint === 'function') bridge.refreshInputSendHint();
  }

  try { localStorage.setItem('claude_client_config', JSON.stringify(bridge._configForPersist?.())); }
  catch (e) { bridge.debugLog?.('[saveSettings] localStorage save failed: ' + (e as Error).message, 'error'); }

}
