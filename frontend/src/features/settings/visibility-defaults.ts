/* ═══════════════════════════════════════════════════════════════════
   settings/visibility-defaults.ts — preset/visibility flags for IG models
   + dropdown models + model defaults.

   Ported from retained settings/visibility_defaults.js. Shared helpers and
   state (_getAllModels, _serverConfig, brand/model-display services) stay
   with their current owners and are reached through the feature registry.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../../feature-registry';
import { escapeHtml } from '../../html-safety';
import { t } from '../../i18n';

interface VisibilityModel {
  model_id: string;
  display_name?: string;
  capabilities?: string[];
}

interface VisibilityProvider {
  name?: string;
  enabled?: boolean;
}

interface VisibilityModelEntry {
  provider: VisibilityProvider;
  model: VisibilityModel;
}

interface VisibilityServerConfig {
  hidden_models?: string[];
  hidden_ig_models?: string[];
  model_defaults?: Record<string, string>;
}

type VisibilityGlobals = Window & {
  _getAllModels?: () => VisibilityModelEntry[];
  _serverConfig?: VisibilityServerConfig | null;
  modelGroupKey?: (provider: VisibilityProvider, model: VisibilityModel) => string;
  modelGroupLabel?: (key: string, fallback?: string) => string;
  modelGroupBrandNames?: () => Record<string, string>;
  isChatModel?: (model: VisibilityModel) => boolean;
  _setHiddenIgModels?: (hidden: Set<string>) => void;
  _setHiddenModels?: (hidden: Set<string>) => void;
  _sortedBrandKeys?: (grouped: Record<string, unknown>, brandNames: Record<string, string>) => string[];
  _sortModelsByDisplayName?: (models: VisibilityModel[]) => void;
  _sortModelEntriesByDisplayName?: (entries: VisibilityModelEntry[]) => void;
  _brandSvg?: (brand: string, size: number) => string;
  _modelShortName?: (modelId: string) => string;
  _warnModelCapsMissing?: () => void;
  _renderPresetsTab?: (cfg?: VisibilityServerConfig | null) => void;
  _collectModelDefaults?: () => Record<string, string>;
  _onIgVisibilityChange?: (checkbox: HTMLInputElement) => void;
  _onDropdownVisibilityChange?: (checkbox: HTMLInputElement) => void;
  _toggleAllIgModels?: (show: boolean) => void;
  _toggleAllDropdownModels?: (show: boolean) => void;
};

function globals(): VisibilityGlobals {
  return featureRegistry as unknown as VisibilityGlobals;
}

function getAllModels(): VisibilityModelEntry[] {
  const collector = globals()._getAllModels;
  return collector ? collector() : [];
}

function serverConfig(): VisibilityServerConfig | null {
  return globals()._serverConfig ?? null;
}


function modelShortName(modelId: string): string {
  const bridge = globals();
  return typeof bridge._modelShortName === 'function' ? bridge._modelShortName(modelId) : modelId;
}

function setSelectOptions(
  select: HTMLElement,
  emptyLabel: string,
  uniqueModels: VisibilityModelEntry[],
  seen: Record<string, boolean>,
  savedVal: string,
): void {
  const sel = select as HTMLSelectElement;
  sel.innerHTML = '<option value="">' + emptyLabel + '</option>';
  for (const entry of uniqueModels) {
    const modelId = entry.model.model_id;
    const opt = document.createElement('option');
    opt.value = modelId;
    opt.textContent = modelShortName(modelId);
    if (modelId === savedVal) opt.selected = true;
    sel.appendChild(opt);
  }
  // If the saved value doesn't match any available model, add it as a custom entry
  if (savedVal && !seen[savedVal]) {
    const customOpt = document.createElement('option');
    customOpt.value = savedVal;
    customOpt.textContent = t('settings.vdUnregistered', { model: savedVal });
    customOpt.selected = true;
    sel.appendChild(customOpt);
  }
}

interface BrandGroup {
  name: string;
  models: VisibilityModel[];
}

function groupByBrand(entries: VisibilityModelEntry[]): {
  grouped: Record<string, BrandGroup>;
  brandNames: Record<string, string>;
} {
  const bridge = globals();
  const grouped: Record<string, BrandGroup> = {};
  for (const entry of entries) {
    const bkey = bridge.modelGroupKey ? bridge.modelGroupKey(entry.provider, entry.model) : '';
    const bname = bridge.modelGroupLabel ? bridge.modelGroupLabel(bkey, entry.provider.name) : (entry.provider.name || bkey);
    if (!grouped[bkey]) grouped[bkey] = { name: bname, models: [] };
    grouped[bkey].models.push(entry.model);
  }
  return { grouped, brandNames: bridge.modelGroupBrandNames ? bridge.modelGroupBrandNames() : {} };
}

function renderVisibilityGroups(
  container: HTMLElement,
  entries: VisibilityModelEntry[],
  hidden: Set<string>,
  rowHtml: (model: VisibilityModel, isVisible: boolean) => string,
): void {
  const bridge = globals();
  const { grouped, brandNames } = groupByBrand(entries);
  let html = '';
  const brandKeys = bridge._sortedBrandKeys
    ? bridge._sortedBrandKeys(grouped, brandNames)
    : Object.keys(grouped).sort();
  for (const brand of brandKeys) {
    const group = grouped[brand];
    if (bridge._sortModelsByDisplayName) bridge._sortModelsByDisplayName(group.models);
    const displayName = brandNames[brand] || group.name || brand;
    html += '<div class="stg-dv-group">';
    html += '<div class="stg-dv-brand">' + (bridge._brandSvg ? bridge._brandSvg(brand, 14) : '')
      + ' <span>' + escapeHtml(displayName) + '</span></div>';
    for (const model of group.models) {
      html += rowHtml(model, !hidden.has(model.model_id));
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

// ══════════════════════════════════════════════════════
//  Preset Tab — visibility controls for image gen & model dropdown
// ══════════════════════════════════════════════════════

function renderPresetsTab(cfg?: VisibilityServerConfig | null): void {
  // Render image gen visibility toggles (same pattern as Model Dropdown)
  renderIgVisibility();
  // Render dropdown visibility toggles
  renderDropdownVisibility();
  // Render model defaults (fallback model, preset defaults)
  populateModelDefaults(cfg);
}

/* The settings visibility lists are the MANAGEMENT surface: every model_id
 * stays a flat, individually toggleable row. Display folds (alias mirrors /
 * version families) belong to the toolbar picker only — folding them here
 * hid rows the user came to show/hide (owner call 2026-08-23). */

// ══════════════════════════════════════════════════════
//  Image Generation Visibility — choose which models show in the image gen picker
// ══════════════════════════════════════════════════════

function renderIgVisibility(): void {
  const container = document.getElementById('stgIgVisibility');
  if (!container) return;

  // Collect all image_gen models from enabled providers
  const igModels = getAllModels().filter((entry) => {
    if (entry.provider.enabled === false) return false;
    const caps = entry.model.capabilities || [];
    return caps.includes('image_gen');
  });

  if (igModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">' + t('settings.vdNoIgModels') + '</p>';
    return;
  }

  // Deduplicate by model_id
  const seen: Record<string, boolean> = {};
  const unique: VisibilityModelEntry[] = [];
  for (const entry of igModels) {
    const mid = entry.model.model_id;
    if (!seen[mid]) {
      seen[mid] = true;
      unique.push(entry);
    }
  }

  // Load hidden set from server config
  const cfg = serverConfig();
  const hidden = new Set((cfg && cfg.hidden_ig_models) || []);

  renderVisibilityGroups(container, unique, hidden, (m, isVisible) => {
    const mid = m.model_id;
    const shortName = modelShortName(mid);
    let h = '<div class="stg-dv-item">';
    h += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
    h += '  <label class="stg-toggle stg-dv-toggle">';
    h += '    <input type="checkbox" data-ig-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' data-tofu-action-change="_onIgVisibilityChange(this)">';
    h += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
    h += '  </label>';
    h += '</div>';
    return h;
  });
}

function onIgVisibilityChange(checkbox: HTMLInputElement): void {
  const modelId = checkbox.getAttribute('data-ig-model-id') || '';
  const cfg = serverConfig();
  const hidden = new Set((cfg && cfg.hidden_ig_models) || []);
  if (checkbox.checked) {
    hidden.delete(modelId);
  } else {
    hidden.add(modelId);
  }
  const arr = Array.from(hidden);
  if (cfg) cfg.hidden_ig_models = arr;
  // Update the global set so image gen picker reflects changes on close
  globals()._setHiddenIgModels?.(hidden);
}

function toggleAllIgModels(show: boolean): void {
  const container = document.getElementById('stgIgVisibility');
  if (!container) return;
  const checkboxes = container.querySelectorAll('input[type="checkbox"][data-ig-model-id]');
  const hidden = new Set<string>();
  checkboxes.forEach((cb) => {
    (cb as HTMLInputElement).checked = show;
    if (!show) hidden.add(cb.getAttribute('data-ig-model-id') || '');
  });
  const arr = Array.from(hidden);
  const cfg = serverConfig();
  if (cfg) cfg.hidden_ig_models = arr;
  globals()._setHiddenIgModels?.(hidden);
}

// ══════════════════════════════════════════════════════
//  Model Dropdown Visibility — choose which models show in the picker
// ══════════════════════════════════════════════════════

function renderDropdownVisibility(): void {
  const container = document.getElementById('stgDropdownVisibility');
  if (!container) return;

  // Collect all chat models from all enabled providers. isChatModel comes
  // from the typed capability-taxonomy owner.
  // Guard: a stale/incomplete bundle can strand this filter without
  // isChatModel(); degrade to "show everything" instead of throwing and
  // leaving the settings list empty. Same rationale as main_toolbar_ui.
  const bridge = globals();
  const hasCaps = (typeof bridge.isChatModel === 'function');
  if (!hasCaps && typeof bridge._warnModelCapsMissing === 'function') bridge._warnModelCapsMissing();
  const allModels = getAllModels().filter((entry) => {
    if (entry.provider.enabled === false) return false;
    return hasCaps && bridge.isChatModel ? bridge.isChatModel(entry.model) : true;
  });

  if (allModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">' + escapeHtml(t('settings.vdNoChatModels')) + '</p>';
    return;
  }

  // Load hidden set from server config (synced at openSettings)
  const cfg = serverConfig();
  const hidden = new Set((cfg && cfg.hidden_models) || []);

  renderVisibilityGroups(container, allModels, hidden, (m, isVisible) => {
    const mid = m.model_id;
    const shortName = modelShortName(mid);
    let h = '<div class="stg-dv-item">';
    h += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
    h += '  <label class="stg-toggle stg-dv-toggle">';
    h += '    <input type="checkbox" data-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' data-tofu-action-change="_onDropdownVisibilityChange(this)">';
    h += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
    h += '  </label>';
    h += '</div>';
    return h;
  });
}

function onDropdownVisibilityChange(checkbox: HTMLInputElement): void {
  const modelId = checkbox.getAttribute('data-model-id') || '';
  const cfg = serverConfig();
  const hidden = new Set((cfg && cfg.hidden_models) || []);
  if (checkbox.checked) {
    hidden.delete(modelId);
  } else {
    hidden.add(modelId);
  }
  const arr = Array.from(hidden);
  // Update cached server config so subsequent toggles are consistent
  if (cfg) cfg.hidden_models = arr;
  // Update the global set so dropdown reflects changes on close
  globals()._setHiddenModels?.(hidden);
}

function toggleAllDropdownModels(show: boolean): void {
  const container = document.getElementById('stgDropdownVisibility');
  if (!container) return;
  const checkboxes = container.querySelectorAll('input[type="checkbox"][data-model-id]');
  const hidden = new Set<string>();
  checkboxes.forEach((cb) => {
    (cb as HTMLInputElement).checked = show;
    if (!show) hidden.add(cb.getAttribute('data-model-id') || '');
  });
  const arr = Array.from(hidden);
  const cfg = serverConfig();
  if (cfg) cfg.hidden_models = arr;
  globals()._setHiddenModels?.(hidden);
}

// ══════════════════════════════════════════════════════
//  Model Defaults — fallback model + preset defaults
// ══════════════════════════════════════════════════════

/**
 * Populate the Model Defaults section (fallback model, preset default models).
 * Uses all chat models from all enabled providers as options.
 */
function populateModelDefaults(cfg?: VisibilityServerConfig | null): void {
  // Collect all chat models through the typed capability-taxonomy owner.
  // Guard: see renderDropdownVisibility above — same failure mode.
  const bridge = globals();
  const hasCapsDef = (typeof bridge.isChatModel === 'function');
  if (!hasCapsDef && typeof bridge._warnModelCapsMissing === 'function') bridge._warnModelCapsMissing();
  const chatModels = getAllModels().filter((entry) => {
    if (entry.provider.enabled === false) return false;
    return hasCapsDef && bridge.isChatModel ? bridge.isChatModel(entry.model) : true;
  });

  // Deduplicate by model_id
  const seen: Record<string, boolean> = {};
  const uniqueModels: VisibilityModelEntry[] = [];
  for (const entry of chatModels) {
    const mid = entry.model.model_id;
    if (!seen[mid]) {
      seen[mid] = true;
      uniqueModels.push(entry);
    }
  }

  // Order the options by the DISPLAY name shown in the <select>, via the ONE
  // shared typed model-display comparator. These previously inherited
  // whatever order _getAllModels walked the provider arrays in — model_id
  // order, which the settings cold sort writes back — while the option TEXT is
  // _modelShortName. Models with no MODEL_PRICING entry render their raw id and
  // so landed at arbitrary positions among the friendly-named ones.
  if (bridge._sortModelEntriesByDisplayName) bridge._sortModelEntriesByDisplayName(uniqueModels);

  // Read saved model_defaults from config
  const defaults = (cfg && cfg.model_defaults) || {};

  // Populate each select element
  const selectors = [
    { id: 'settingFallbackModel', key: 'fallback_model', emptyLabel: t('settings.vdFallbackEmpty') },
    { id: 'settingDefaultModel', key: 'default_model', emptyLabel: t('settings.vdDefaultEmpty') },
  ];

  for (const selector of selectors) {
    const sel = document.getElementById(selector.id);
    if (!sel) continue;
    const savedVal = defaults[selector.key] || '';
    setSelectOptions(sel, selector.emptyLabel, uniqueModels, seen, savedVal);
  }
}

/**
 * Collect current model defaults from the UI for saving.
 */
function collectModelDefaults(): Record<string, string> {
  const result: Record<string, string> = {};
  const fields = [
    { id: 'settingFallbackModel', key: 'fallback_model' },
    { id: 'settingDefaultModel', key: 'default_model' },
  ];
  for (const field of fields) {
    const el = document.getElementById(field.id);
    if (el instanceof HTMLSelectElement || el instanceof HTMLInputElement) {
      result[field.key] = el.value || '';
    }
  }
  return result;
}

const bridge = globals();
bridge._renderPresetsTab = renderPresetsTab;
bridge._collectModelDefaults = collectModelDefaults;
bridge._onIgVisibilityChange = onIgVisibilityChange;
bridge._onDropdownVisibilityChange = onDropdownVisibilityChange;
bridge._toggleAllIgModels = toggleAllIgModels;
bridge._toggleAllDropdownModels = toggleAllDropdownModels;
