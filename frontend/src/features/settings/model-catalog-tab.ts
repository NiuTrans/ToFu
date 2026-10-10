/*
 * Models settings tab — Creator/Model facts and the shared reasoning baseline.
 *
 * Responsibility: own everything rendered into ``#settingsTab_models``
 * (static/settings_panels/models.html). The page answers exactly one
 * question: which official models exist and what reasoning they support,
 * grouped by Creator. Provider supply, credentials, offerings, deployments, aliases,
 * and routes never render here — the projection built below is a real data
 * boundary, not a view convention: the typed Model feature
 * (frontend/src/features/model-catalog/) receives only Creator/Model fields
 * even though the staged authority document holds the rest.
 *
 * The 服务商 (Providers) tab is the mirror image: the retained
 * settings/provider_render.js owns provider supply and never re-renders this
 * page. Flows that reload the v2 authority or mutate creators/models call
 * ``_renderModelRoutingTabs()`` so both pages repaint by name, not as a side
 * effect.
 */

import { featureRegistry } from '../../feature-registry';
import { escapeHtml } from '../../html-safety';
import type { ModelRoutingInferenceProfile } from '../../api/api-v1-model-routing.generated';

interface ModelCatalogPricing {
  input?: unknown;
  output?: unknown;
  currency?: unknown;
  unit?: unknown;
  cache_read?: unknown;
  cache_write?: unknown;
}

interface ModelCatalogModel {
  inference_profile?: ModelRoutingInferenceProfile;
  creator_id?: string;
  model_id?: string;
  display_name?: string;
  capabilities?: string[];
  context_window?: unknown;
  quality_rank?: unknown;
  release_date?: string;
  registration_state?: 'pending_metadata' | 'pending_validation' | 'ready' | 'disabled';
  registration_issues?: string[];
  list_pricing?: ModelCatalogPricing;
  official_pricing?: ModelCatalogPricing;
  lifecycle?: unknown;
}

interface ModelCatalogCreator {
  creator_id?: string;
  name?: string;
}

interface ModelCatalogRoutingDocument {
  contract_version?: string;
  creators?: ModelCatalogCreator[];
  models?: ModelCatalogModel[];
}

type ModelCatalogGlobals = Window & {
  _stgModelRouting?: ModelCatalogRoutingDocument | null;
  _stgModelRoutingLoadError?: string;
  _renderProvidersTab?: () => void;
  _renderModelCatalogPanel?: (documentValue: unknown, stageReasoning?:
    (creator: string, model: string, profile: ModelRoutingInferenceProfile) => void) => void;
  _setModelCatalogSearchOwner?: (value: unknown) => void;
  _brandSvg?: (brand: string, size: number) => string;
  _modelBrand?: (modelId: string, creatorId?: string) => string;
  _detectBrand?: (text: string) => string;
  _renderModelRoutingTabs?: () => void;
  _renderModelCatalogTab?: () => void;
  _setModelCatalogSearch?: (value: unknown) => void;
};

function globals(): ModelCatalogGlobals {
  return featureRegistry as unknown as ModelCatalogGlobals;
}

/** Search text staged by the page's search box (data-tofu-action-input in
 *  static/settings_panels/models.html). Kept across re-renders so typing in
 *  either tab never drops the query. */
let _stgModelCatalogQuery = '';

/** Repaint both model-routing settings tabs. Use this after any flow that
 *  reloads the v2 authority or changes creators/models (settings open,
 *  subscription sync, template bundle add, provider delete). Pure
 *  provider-side edits (credentials, connections, offerings, matrix grants)
 *  call ``_renderProvidersTab()`` alone — the registry does not change. */
function renderModelRoutingTabs(): void {
  globals()._renderProvidersTab?.();
  renderModelCatalogTab();
}

function setModelCatalogSearch(value: unknown): unknown {
  const owner = globals()._setModelCatalogSearchOwner;
  if (typeof owner === 'function') return owner(value);
  _stgModelCatalogQuery = String(value || '');
  renderModelCatalogTab();
  return undefined;
}

function renderModelCatalogTab(): void {
  const list = document.getElementById('stgModelCatalog');
  if (!list) return;
  const routing = globals()._stgModelRouting;
  if (!routing) {
    const loadError = globals()._stgModelRoutingLoadError;
    list.innerHTML = '<p class="stg-empty">' + escapeHtml(loadError
      ? '加载模型目录失败：' + loadError : '正在加载模型目录…') + '</p>';
    return;
  }
  // This is an actual data boundary, not just a view convention: the Model
  // feature receives a fresh Creator/Model-only projection, so provider-side
  // fields are unavailable to it even if the authority document contains them.
  const catalogDocument = {
    contract_version: routing.contract_version,
    creators: (routing.creators || []).map((creator) => ({
      creator_id: creator.creator_id,
      name: creator.name,
    })),
    // Drafts are persisted for admission/research but are not official
    // registrations. Do not present provider-derived pending rows as Models.
    // Older unmarked rows remain visible until their migration classifies them.
    models: (routing.models || []).filter((model) => !model.registration_state
      || model.registration_state === 'ready').map((model) => {
      // Derived vendor knowledge (release_date, official_pricing) is stamped
      // by the backend at the read boundary; forward both verbatim so the
      // typed feature can fall back to the official price table when the
      // row carries no registered list_pricing.
      const pricing = model.list_pricing;
      const official = model.official_pricing;
      const copyPricing = (source: ModelCatalogPricing | undefined) => (source ? {
        input: source.input,
        output: source.output,
        currency: source.currency,
        unit: source.unit,
        cache_read: source.cache_read,
        cache_write: source.cache_write,
      } : undefined);
      return {
        creator_id: model.creator_id,
        model_id: model.model_id,
        display_name: model.display_name,
        capabilities: (model.capabilities || []).slice(),
        context_window: model.context_window,
        inference_profile: model.inference_profile,
        quality_rank: model.quality_rank,
        release_date: model.release_date || undefined,
        registration_state: model.registration_state, registration_issues: model.registration_issues,
        list_pricing: copyPricing(pricing),
        official_pricing: copyPricing(official),
        lifecycle: model.lifecycle,
      };
    }),
  };
  const owner = globals()._renderModelCatalogPanel;
  if (typeof owner === 'function') {
    owner(catalogDocument, (creator, modelId, profile) => {
      const model = globals()._stgModelRouting?.models?.find(row =>
        row.creator_id === creator && row.model_id === modelId);
      if (!model) throw new Error('Model no longer exists');
      model.inference_profile = profile;
    });
    return;
  }
  // The typed owner may be absent in an embedded/test shell. Its fallback is
  // still Model-only: never infer model facts from provider supply or aliases.
  const documentValue = catalogDocument;
  const query = _stgModelCatalogQuery.trim().toLowerCase();
  const rows = (documentValue.models || []).filter((model) => {
    const haystack = [model.display_name, model.model_id, model.creator_id].join(' ').toLowerCase();
    return !query || haystack.includes(query);
  }).sort((left, right) => String(left.display_name || left.model_id).localeCompare(
    String(right.display_name || right.model_id), undefined,
    { numeric: true, sensitivity: 'base' }));
  const visible = rows.slice(0, 120);
  const input = document.getElementById('stgModelCatalogSearch');
  if (input && (input as HTMLInputElement).value !== _stgModelCatalogQuery) {
    (input as HTMLInputElement).value = _stgModelCatalogQuery;
  }
  const bridge = globals();
  list.innerHTML = '<div class="stg-v2-catalog-count">' + rows.length + ' 个官方模型' +
    (query ? ' 匹配当前搜索' : '') + '</div><div class="stg-v2-catalog-list">' +
    visible.map((model) => '<article class="stg-v2-catalog-row"><div class="stg-v2-catalog-icon">' +
      (bridge._brandSvg ? bridge._brandSvg(typeof bridge._modelBrand === 'function'
        ? bridge._modelBrand(model.model_id || '', model.creator_id)
        : (bridge._detectBrand ? bridge._detectBrand((model.creator_id || '') + ' ' + (model.model_id || '')) : ''), 18) : '') + '</div>' +
      '<div class="stg-v2-catalog-identity"><strong>' + escapeHtml(String(model.display_name || model.model_id)) +
      '</strong><span>' + escapeHtml(String(model.creator_id) + '/' + String(model.model_id)) + '</span></div></article>').join('') + '</div>';
}

const bridge = globals();
bridge._renderModelRoutingTabs = renderModelRoutingTabs;
bridge._renderModelCatalogTab = renderModelCatalogTab;
bridge._setModelCatalogSearch = setModelCatalogSearch;
