/* ═══════════════════════════════════════════════════════════════════
   settings/save-export.ts — close/save/export/import coordination.
   save-input.ts captures form values; save-feedback.ts owns the save latch
   and view feedback; config-import.ts coordinates file imports through that
   latch. Routing transactions retain their common authority.

   Ported from retained settings/save_export.js. Shared settings state and
   the collectors owned by still-retained sections are reached through the
   feature registry (the same object the lazy presenters bundle uses as its
   runtimeScope), keeping one authority for each value.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import {retireSettingsReads} from './panel-read-session';
import {beginSettingsSaveFeedback, retireSettingsSaveFeedback, type SettingsSaveFeedback} from './save-feedback';
import {captureSettingsSaveInput, settingsSaveInputChanged, applyLocalSettingsInput, type SettingsSaveInput, type SettingsFeatureFlag} from './save-input';
import {readServerConfig} from './server-config-reader';
import {importServerConfig} from './config-import';
import { hasPendingModelRoutingChanges, persistModelRoutingCredentialSecrets, type ModelRoutingMutationResult } from './model-routing-authority';
import {markSearchGatewayKeySaved} from './search-gateway';
import type {SttSaveIntent} from './speech';

interface SaveExportFeatureFlags {
  trading_enabled?: boolean;
  pptx_translate_enabled?: boolean;
  debug_mode?: boolean;
  optimizer_enabled?: boolean;
}

type SaveExportGlobals = Window & {
  Api?: {
    features?: { set(flags: Record<string, boolean>): Promise<{ json(): Promise<{ ok?: boolean }> } | null> };
    serverConfig?: { update(payload: unknown): Promise<{ json(): Promise<{ ok?: boolean; error?: string }> } | null> };
  };
  config?: Record<string, unknown>;
  debugLog?: (message: string, level?: string) => void;
  renderConversationList?: () => void;
  getActiveConv?: () => { id: string } | null;
  requestAuthoritativeConversationRender?: (id: string, options: { forceScroll: boolean }) => void;
  _featureFlags?: SaveExportFeatureFlags;
  _registeredModels?: unknown[];
  _populateModelDropdown?: (models: unknown[]) => void;
  _applyModelUI?: (model: unknown) => void;
  _loadIgModels?: () => Promise<void> | void;
  _applyTradingVisibility?: () => void;
  _applyDebugModeVisibility?: () => void;
  _loadServerConfigAndPopulate?: () => void;
  _refreshServerConfigAfterSave?: () => Promise<void>;
  _captureSttSaveIntent?: () => SttSaveIntent;
  _persistSttProvider?: (intent?: SttSaveIntent) => Promise<ModelRoutingMutationResult | void>;
  _hasSttChanges?: () => boolean;
  _destroySkills?: () => void;
  _destroyPreferences?: () => void;
  openSettings?: () => void;
  closeSettings?: () => void;
  saveSettings?: () => Promise<void>;
  exportServerConfig?: () => void;
  importServerConfig?: (event: Event) => Promise<void>;
  // Still-retained settings owners (lazy presenters bundle).
  _serverConfig?: Record<string, unknown> | null;
  _stgModelRouting?: unknown;
  _stgModelRoutingLoadError?: string;
  _stgModelRoutingLoadPromise?: Promise<unknown> | null;
  _loadModelRoutingAuthority?: () => Promise<unknown>;
  _persistModelRoutingNow?: () => Promise<unknown>;
};

function globals(): SaveExportGlobals {
  return featureRegistry as unknown as SaveExportGlobals;
}

// ══════════════════════════════════════════════════════
//  Close / Save / Export / Import
// ══════════════════════════════════════════════════════

function closeSettings(): void {
  retireSettingsReads();
  retireSettingsSaveFeedback();
  const bridge = globals() as unknown as Record<string, unknown>;
  for (const name of [
    '_destroyPrivateHosts', '_destroyBrowserAccess',
    '_destroyDevicesTab',
    '_destroySpeechTab', '_destroyCatpawSettings', '_destroySubscriptionPlans',
    '_destroyCredentialsVault', '_destroyAuthSources',
    '_destroyKnowledgeTab',
    '_destroyModelCatalogPanel',

    '_destroyProviderTemplatePicker',
    '_closeProviderModelPicker',
    '_destroyProviderCredentialViews',
  ]) {
    const cleanup = bridge[name];
    if (typeof cleanup === 'function') (cleanup as () => void)();
  }
  document.getElementById('settingsModal')?.classList.remove('open');
  // Refresh model dropdown to reflect any visibility changes
  if (typeof bridge._populateModelDropdown === 'function' && (bridge._registeredModels as unknown[]).length > 0) {
    (bridge._populateModelDropdown as (models: unknown[]) => void)(bridge._registeredModels as unknown[]);
    (bridge._applyModelUI as (model: unknown) => void)((bridge.config as Record<string, unknown>).model);
  }
  // Refresh image gen picker to reflect visibility changes
  if (typeof bridge._loadIgModels === 'function') {
    void (bridge._loadIgModels as () => Promise<void>)();
  }
}

function settingsSaveFailed(error: unknown, feedback: SettingsSaveFeedback): void {
  const message = error && (error as Error).message ? (error as Error).message : String(error);
  globals().debugLog?.('[Settings] Save failed: ' + message, 'error');
  feedback.show('保存失败：' + message);
}

async function saveSettings(): Promise<void> {
  const feedback = beginSettingsSaveFeedback();
  if (!feedback) return;
  try {
    const session = feedback.session;
    if (session) {
      const ready = await session.ready;
      if (!session.isCurrent()) return;
      if (!ready) throw new Error(t('settings.serverConfigFailed'));
    }
    const input = captureSettingsSaveInput();
    const speech = input.server ? globals()._captureSttSaveIntent?.() : undefined;
    feedback.watchSubmittedForm();
    await saveSettingsBody(input, feedback, speech);
  } catch (error) {
    settingsSaveFailed(error, feedback);
  } finally {
    feedback.finish();
  }
}

async function persistFeatureFlag(
  flag: keyof SaveExportFeatureFlags,
  newVal: boolean,
  onApplied?: () => void,
): Promise<void> {
  const bridge = globals();
  const response = await bridge.Api?.features?.set({ [flag]: newVal });
  const data = response ? await response.json() : null;
  if (data?.ok !== true) throw new Error(t('settings.featureSaveFailed'));
  const labels: Record<string, [string, string]> = {
    trading_enabled: ['Trading module enabled — applied', 'Trading module disabled — applied'],
    pptx_translate_enabled: ['PPTX translate enabled', 'PPTX translate disabled'],
    debug_mode: ['Debug mode enabled', 'Debug mode disabled'],
    optimizer_enabled: ['Daily Optimizer enabled', 'Daily Optimizer disabled'],
  };
  const label = labels[flag];
  bridge.debugLog?.(newVal ? label[0] : label[1], 'success');
  if (bridge._featureFlags) bridge._featureFlags[flag] = newVal;
  onApplied?.();
}

function applyFeatureFlag(flag: SettingsFeatureFlag, value: boolean): void {
  const bridge = globals();
  if (flag === 'trading_enabled') bridge._applyTradingVisibility?.();
  if (flag === 'debug_mode') {
    bridge._applyDebugModeVisibility?.();
    bridge.renderConversationList?.();
    const conversation = bridge.getActiveConv?.();
    if (conversation) bridge.requestAuthoritativeConversationRender?.(conversation.id, {forceScroll: true});
  }
  if (flag === 'optimizer_enabled') {
    const badge = document.getElementById('optimizerBadge');
    if (badge) badge.style.display = value ? 'inline-flex' : 'none';
  }
}

async function saveSettingsBody(
  input: SettingsSaveInput, feedback: SettingsSaveFeedback, speechIntent?: SttSaveIntent,
): Promise<void> {
  const bridge = globals();
  applyLocalSettingsInput();
  for (const {flag, value} of input.flags) {
    const current = bridge._featureFlags?.[flag];
    const previous = flag === 'optimizer_enabled' && current === undefined ? true : !!current;
    if (value !== previous) await persistFeatureFlag(flag, value, () => applyFeatureFlag(flag, value));
  }
  if (input.server) {
    // Routing retains its common draft/CAS queue and credential settlement.
    if (bridge._stgModelRoutingLoadPromise || !bridge._stgModelRouting) {
      const ready = await bridge._loadModelRoutingAuthority?.();
      if (!ready) throw new Error(bridge._stgModelRoutingLoadError || 'model-routing v2 authority is unavailable');
    }
    const speech = await bridge._persistSttProvider?.(speechIntent);
    if (speech && !speech.refreshed) {
      feedback.show(t('settings.providerSavedDraftKept'));
      return;
    }
    if (!await saveServerConfig(input, feedback)) return;
  } else if (!feedback.isCurrent() || feedback.wasEdited() || settingsSaveInputChanged(input)) {
    feedback.show(`${t('settings.saved')} · ${t('context.unsaved')}`);
    return;
  }
  bridge.debugLog?.('Settings saved', 'success');
  if (feedback.isCurrent()) closeSettings();
}

async function saveServerConfig(input: SettingsSaveInput, feedback: SettingsSaveFeedback): Promise<boolean> {
  const bridge = globals();
  const payload = input.server;
  if (!payload || !bridge._stgModelRouting) throw new Error('model-routing v2 authority is not loaded');
  await bridge._persistModelRoutingNow?.();
  const credentials = await persistModelRoutingCredentialSecrets();
  const response = await bridge.Api?.serverConfig?.update(payload);
  const data = response ? await response.json() : null;
  if (!data?.ok) throw new Error(data?.error || 'server-config save returned no acknowledgement');

  // Check current intent before acknowledging a submitted plaintext field.
  const edited = !feedback.isCurrent() || feedback.wasEdited() || settingsSaveInputChanged(input);
  if (feedback.isCurrent() && payload.search.gateway) markSearchGatewayKeySaved(payload.search.gateway);
  bridge.debugLog?.('[Settings] ' + t('settings.configSaved'), 'success');
  feedback.show(t('settings.saved'));
  // This is committed application policy, independent of the editor lifetime.
  if (typeof bridge._refreshServerConfigAfterSave === 'function') {
    void bridge._refreshServerConfigAfterSave().catch(() => { /* startup recovery owns retries */ });
  } else bridge._loadServerConfigAndPopulate?.();
  if (edited || !credentials.refreshed || hasPendingModelRoutingChanges() || bridge._hasSttChanges?.()) {
    feedback.show(credentials.refreshed
      ? `${t('settings.saved')} · ${t('context.unsaved')}` : t('settings.providerSavedDraftKept'));
    return false;
  }
  return true;
}

function exportServerConfig(): void {
  const bridge = globals();
  void readServerConfig().then((cfg) => {
    if (!cfg) return;
    const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'tofu-config-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    bridge.debugLog?.('[Settings] Config exported', 'success');
  });
}


const bridge = globals();
bridge.closeSettings = closeSettings;
bridge.saveSettings = saveSettings;
bridge.exportServerConfig = exportServerConfig;
bridge.importServerConfig = importServerConfig;
