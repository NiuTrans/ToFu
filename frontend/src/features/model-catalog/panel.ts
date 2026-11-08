/**
 * Creator/Model Settings catalog with an injected reasoning-draft edit port.
 *
 * Entry points: renderModelCatalogPanel, setModelCatalogSearch,
 * repaintModelCatalogPanel, invalidateModelCatalogAaScores,
 * destroyModelCatalogPanel. Dependencies are the v2
 * Creator/Model facts plus shared brand and price presentation. Provider,
 * Offering, Deployment, alias, route, and credential state are prohibited.
 */

import './model-catalog.css';
import { brandIconHtml } from '../../core/model-brand-icons';
import {
  createModelCapabilityTaxonomy,
  type ModelCapabilityTaxonomy,
} from '../../core/model-capability-taxonomy';
import { featureRegistry } from '../../feature-registry';
import { _i18nLang, t } from '../../i18n';
import { modelPricePresentation } from '../settings/model-price-localization';
import { fetchAaScores, refreshAaScores, saveAaKey } from './api';
import {
  capabilityLabel,
  formatMediaRate,
  kindGlyph,
  kindLabel,
  mediaFactLines,
  mediaPriceSummary,
} from './kinds';
import {
  buildVendorGroups,
  countAaScoredModels,
  countModelKinds,
  filterGroupsByKind,
} from './model';
import { createReasoningEditor } from '../settings/reasoning-editor';
import type { ModelRoutingInferenceProfile } from '../../api/api-v1-model-routing.generated';
import {
  aaIndexVersionSuffix,
  buildParetoPoints,
  closeParetoDialog,
  openParetoDialog,
} from './pareto-chart';
import type {
  AaBlock,
  ModelCatalogDocument,
  ModelCatalogRow,
  ModelPricing,
  VendorGroup,
} from './types';

const MAX_VISIBLE_MODELS = 240;
let activeDocument: ModelCatalogDocument | null = null;
let stageReasoning: ((creator: string, model: string, profile: ModelRoutingInferenceProfile) => void) | undefined;
let searchQuery = '';
let openVendorId: string | null = null;
const openModels = new Set<string>();
let activeAa: AaBlock | null = null;
let aaLoaded = false;
let aaBusy = false;
let aaGuideOpen = false;
let aaError = '';
let aaRequestGeneration = 0;
/** Selected kind chip; null shows every kind. */
let kindFilter: string | null = null;

type KindTaxonomy = Pick<ModelCapabilityTaxonomy, 'modelKind' | 'isMediaKind' | 'getModelKinds'>;
const fallbackTaxonomy: KindTaxonomy = createModelCapabilityTaxonomy();

/** The composition's server-projected taxonomy, else the compiled fallback. */
function taxonomy(): KindTaxonomy {
  const live = featureRegistry as unknown as Partial<KindTaxonomy>;
  return typeof live.modelKind === 'function' && typeof live.isMediaKind === 'function'
    && typeof live.getModelKinds === 'function' ? live as KindTaxonomy : fallbackTaxonomy;
}

function container(): HTMLElement | null {
  return document.getElementById('stgModelCatalog');
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function priceRates(): Record<string, unknown> {
  const scope = window as unknown as Record<string, unknown>;
  const policy = scope._modelPriceDisplayPolicy as { usd_rates?: unknown } | undefined;
  return policy?.usd_rates && typeof policy.usd_rates === 'object'
    ? policy.usd_rates as Record<string, unknown>
    : { USD: 1 };
}

function priceToUsd(value: number, currency: string): number {
  const rate = Number(priceRates()[String(currency || 'USD').toUpperCase()]);
  return Number.isFinite(rate) && rate > 0 ? value / rate : value;
}

function formatPrice(value: unknown, currency: unknown): string {
  return modelPricePresentation.formatForUi(
    value,
    currency ?? 'USD',
    _i18nLang,
    priceRates(),
  );
}

function formatPricingCompact(pricing: ModelPricing | null): string {
  if (!pricing) return '—';
  const input = pricing.input == null ? '—' : formatPrice(pricing.input, pricing.currency);
  const output = pricing.output == null ? '—' : formatPrice(pricing.output, pricing.currency);
  return `${input} / ${output}`;
}

function formatContextWindow(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '—';
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(2))}M`;
  if (value >= 1000) return `${Math.round(value / 1000)}K`;
  return String(Math.round(value));
}

function modelIdentity(row: ModelCatalogRow): string {
  return `${row.creatorId}/${row.modelId}`;
}

function renderParetoEntry(groups: VendorGroup[]): HTMLElement {
  const points = buildParetoPoints(groups, { toUsd: priceToUsd });
  const button = element('button', 'stg-mc-pareto-entry');
  button.type = 'button';
  button.disabled = points.length < 2;
  button.setAttribute('aria-label', '打开成本与 AA Index 图表');
  button.innerHTML = '<span class="stg-mc-pareto-entry-icon" aria-hidden="true">'
    + '<svg viewBox="0 0 32 32"><path d="M5 25V7M5 25h22"/>'
    + '<path class="frontier" d="M8 22l5-5 5 1 8-10"/>'
    + '<circle cx="8" cy="22" r="1.5"/><circle cx="13" cy="17" r="1.5"/>'
    + '<circle cx="18" cy="18" r="1.5"/><circle cx="26" cy="8" r="1.5"/></svg></span>'
    + '<span class="stg-mc-pareto-entry-text">'
    + `<strong>成本 × AA Index${aaIndexVersionSuffix(activeAa?.index_version)}</strong>`
    + `<span>${points.length >= 2
      ? `${points.length} 个模型 · 官方价格 · Pareto 前沿`
      : t('modelCatalog.aa.chartWaiting')}</span></span>`
    + '<svg class="stg-mc-pareto-entry-chevron" viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M9 5l7 7-7 7"/></svg>';
  if (points.length >= 2) {
    button.addEventListener('click', () => openParetoDialog(groups, {
      toUsd: priceToUsd,
      indexVersion: activeAa?.index_version,
    }));
  }
  return button;
}

/** ``scored``: catalog Models with a published score, not raw AA matches. */
function aaStatusCopy(scored: number): { label: string; detail: string; state: string } {
  if (aaBusy && !activeAa) {
    return {
      label: t('modelCatalog.aa.statusLoading'),
      detail: t('modelCatalog.aa.loadingDetail'),
      state: 'loading',
    };
  }
  if (activeAa?.status === 'ok') {
    return {
      label: t('modelCatalog.aa.statusReady'),
      detail: t('modelCatalog.aa.readyDetail', { n: scored }),
      state: 'ready',
    };
  }
  if (activeAa?.status === 'stale') {
    return {
      label: t('modelCatalog.aa.statusStale'),
      detail: t('modelCatalog.aa.staleDetail', { n: scored }),
      state: 'stale',
    };
  }
  if (activeAa?.status === 'no_key') {
    return {
      label: t('modelCatalog.aa.statusNoKey'),
      detail: t('modelCatalog.aa.noKeyDetail'),
      state: 'empty',
    };
  }
  return {
    label: t('modelCatalog.aa.statusUnavailable'),
    detail: t('modelCatalog.aa.unavailableDetail'),
    state: 'error',
  };
}

function aaKeySourceLabel(): string | null {
  if (activeAa?.key_source === 'settings') {
    return t('modelCatalog.aa.keySourceSettings', { hint: activeAa.key_hint || '••••' });
  }
  if (activeAa?.key_source === 'legacy_config') {
    return t('modelCatalog.aa.keySourceLegacy', { hint: activeAa.key_hint || '••••' });
  }
  if (activeAa?.key_source === 'env') {
    return t('modelCatalog.aa.keySourceEnv', { hint: activeAa.key_hint || '••••' });
  }
  return null;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return String(error || t('modelCatalog.aa.unknownError'));
}

async function loadAa(): Promise<void> {
  const generation = ++aaRequestGeneration;
  aaBusy = true;
  aaError = '';
  render();
  try {
    const response = await fetchAaScores();
    if (generation !== aaRequestGeneration) return;
    activeAa = response.aa ?? null;
    aaLoaded = true;
    if (activeAa?.status === 'no_key') aaGuideOpen = true;
  } catch (error) {
    if (generation !== aaRequestGeneration) return;
    aaLoaded = true;
    aaError = errorMessage(error);
  } finally {
    if (generation === aaRequestGeneration) {
      aaBusy = false;
      render();
    }
  }
}

async function refreshAa(): Promise<void> {
  const generation = ++aaRequestGeneration;
  aaBusy = true;
  aaError = '';
  render();
  try {
    const response = await refreshAaScores();
    if (generation !== aaRequestGeneration) return;
    activeAa = response.aa ?? null;
    aaLoaded = true;
  } catch (error) {
    if (generation !== aaRequestGeneration) return;
    aaError = errorMessage(error);
  } finally {
    if (generation === aaRequestGeneration) {
      aaBusy = false;
      render();
    }
  }
}

async function persistAaKey(apiKey: string): Promise<void> {
  const generation = ++aaRequestGeneration;
  aaBusy = true;
  aaError = '';
  render();
  try {
    const response = await saveAaKey(apiKey);
    if (generation !== aaRequestGeneration) return;
    activeAa = response.aa ?? null;
    aaLoaded = true;
    aaGuideOpen = !activeAa?.key_source;
  } catch (error) {
    if (generation !== aaRequestGeneration) return;
    aaError = errorMessage(error);
    aaGuideOpen = true;
  } finally {
    if (generation === aaRequestGeneration) {
      aaBusy = false;
      render();
    }
  }
}

function renderAaSource(scored: number): HTMLElement {
  const status = aaStatusCopy(scored);
  const disclosure = element('details', `stg-mc-aa-source is-${status.state}`);
  disclosure.open = aaGuideOpen || activeAa?.status === 'no_key';
  disclosure.addEventListener('toggle', () => { aaGuideOpen = disclosure.open; });

  const summary = element('summary', 'stg-mc-aa-summary');
  summary.appendChild(element('span', 'stg-mc-aa-mark', 'AA'));
  const identity = element('span', 'stg-mc-aa-identity');
  identity.append(
    element('strong', undefined,
      `${t('modelCatalog.aa.sourceTitle')}${aaIndexVersionSuffix(activeAa?.index_version)}`),
    element('span', undefined, status.detail),
  );
  const state = element('span', `stg-mc-aa-state is-${status.state}`);
  state.append(
    element('span', 'stg-mc-aa-state-dot'),
    element('span', undefined, status.label),
  );
  summary.append(identity, state, element('span', 'stg-mc-aa-configure', t('modelCatalog.aa.configure')));

  const body = element('div', 'stg-mc-aa-body');
  const explanation = element('div', 'stg-mc-aa-explanation');
  const heading = element('strong', undefined, t('modelCatalog.aa.guideTitle'));
  const paragraph = element('p', undefined, t('modelCatalog.aa.guideBody'));
  const link = element('a', undefined, 'artificialanalysis.ai');
  link.href = activeAa?.source_url || 'https://artificialanalysis.ai/';
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  paragraph.append(document.createTextNode(' '), link);
  explanation.append(heading, paragraph);

  const form = element('form', 'stg-mc-aa-form');
  const input = element('input', 'stg-mc-aa-key') as HTMLInputElement;
  input.type = 'password';
  input.autocomplete = 'new-password';
  input.maxLength = 256;
  input.placeholder = activeAa?.key_source
    ? t('modelCatalog.aa.replacePlaceholder')
    : t('modelCatalog.aa.keyPlaceholder');
  input.disabled = aaBusy;
  const save = element('button', 'stg-mc-aa-primary',
    aaBusy ? t('modelCatalog.aa.working') : t('modelCatalog.aa.saveAndSync'));
  save.type = 'submit';
  save.disabled = aaBusy;
  form.append(input, save);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) {
      aaError = t('modelCatalog.aa.keyRequired');
      aaGuideOpen = true;
      render();
      return;
    }
    void persistAaKey(value);
  });

  const controls = element('div', 'stg-mc-aa-controls');
  const keySource = aaKeySourceLabel();
  if (keySource) controls.appendChild(element('span', 'stg-mc-aa-key-source', keySource));
  if (activeAa?.key_source) {
    const refresh = element('button', 'stg-mc-aa-link', t('modelCatalog.aa.refreshNow'));
    refresh.type = 'button';
    refresh.disabled = aaBusy;
    refresh.addEventListener('click', () => { void refreshAa(); });
    controls.appendChild(refresh);
  }
  if (activeAa?.key_source === 'settings' || activeAa?.key_source === 'legacy_config') {
    const clear = element('button', 'stg-mc-aa-link is-danger', t('modelCatalog.aa.removeKey'));
    clear.type = 'button';
    clear.disabled = aaBusy;
    clear.addEventListener('click', () => {
      if (window.confirm(t('modelCatalog.aa.removeConfirm'))) void persistAaKey('');
    });
    controls.appendChild(clear);
  }
  body.append(explanation, form);
  if (controls.childElementCount) body.append(controls);
  if (aaError) body.appendChild(element('p', 'stg-mc-aa-error', aaError));
  disclosure.append(summary, body);
  return disclosure;
}

function renderMetric(label: string, value: string, kind?: string): HTMLElement {
  const fact = element('div', `stg-mc-fact${kind ? ` ${kind}` : ''}`);
  fact.append(
    element('span', 'stg-mc-fact-label', label),
    element('strong', 'stg-mc-fact-value', value),
  );
  return fact;
}

function renderModel(row: ModelCatalogRow, editors: Map<string, HTMLElement>): HTMLElement {
  const identity = modelIdentity(row);
  const card = element('details', 'stg-mc-model');
  card.open = openModels.has(identity);
  card.addEventListener('toggle', () => {
    if (card.open) openModels.add(identity);
    else openModels.delete(identity);
  });

  const media = taxonomy().isMediaKind(row.kind);
  const summary = element('summary', 'stg-mc-model-head');
  const title = element('div', 'stg-mc-model-title');
  title.innerHTML = brandIconHtml(row.brand, 21);
  title.appendChild(element('strong', 'stg-mc-model-name', row.displayName));
  if (row.kind !== 'chat') title.appendChild(renderKindBadge(row.kind));
  const brief = element('div', 'stg-mc-model-brief');
  if (row.registrationState && row.registrationState !== 'ready') {
    const state = element('span', 'stg-mc-meta', t(row.registrationState === 'disabled'
      ? 'modelCatalog.registrationDisabled' : 'modelCatalog.registrationPending'));
    state.title = (row.registrationIssues ?? []).join('; ');
    brief.appendChild(state);
  }
  // The AA Intelligence Index scores conversation models only.
  const intelligence = row.aa?.intelligence;
  if (row.kind === 'chat' && intelligence !== null && intelligence !== undefined) {
    const score = element('span', 'stg-mc-score', `AA ${Number(intelligence.toFixed(1))}`);
    score.title = t('modelCatalog.aa.scoreMatched', {
      score: Number(intelligence.toFixed(1)),
      name: row.aa?.aa_name || row.displayName,
    });
    brief.appendChild(score);
  } else if (row.kind === 'chat') {
    brief.appendChild(element('span', 'stg-mc-score na', 'AA —'));
  }
  // A media kind is described by its media facts; a token context window
  // says nothing about an image, video or speech model.
  if (!media) {
    const context = element('span', 'stg-mc-meta', formatContextWindow(row.contextWindow));
    context.title = '上下文窗口';
    brief.appendChild(context);
  }
  const release = element('span', 'stg-mc-meta', row.releaseDate || '—');
  release.title = '发布时间';
  const mediaPrice = media
    ? mediaPriceSummary(row.mediaPricing, (value) => formatPrice(value, row.mediaPricing?.currency))
    : null;
  const price = element('span', 'stg-mc-meta', mediaPrice ?? formatPricingCompact(row.pricing));
  price.title = mediaPrice ? t('modelCatalog.media.priceTitle') : '输入 / 输出 · 每 1M tokens';
  brief.append(release, price);
  const caret = element('span', 'stg-mc-caret');
  caret.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5"/></svg>';
  summary.append(title, brief, caret);

  const detail = element('div', 'stg-mc-model-detail');
  const facts = element('div', 'stg-mc-model-facts');
  facts.append(
    renderMetric('模型 ID', identity, 'id'),
    renderMetric('生命周期', row.lifecycle || 'stable'),
  );
  detail.appendChild(facts);
  if (row.aa && row.kind === 'chat') {
    detail.appendChild(element(
      'p',
      'stg-mc-score-source',
      t('modelCatalog.aa.scoreDetail', {
        name: row.aa.aa_name || row.displayName,
        coding: row.aa.coding == null ? '—' : Number(row.aa.coding.toFixed(1)),
        agentic: row.aa.agentic == null ? '—' : Number(row.aa.agentic.toFixed(1)),
      }),
    ));
  }
  if (row.capabilities.length) {
    const capabilities = element('div', 'stg-mc-capabilities');
    capabilities.appendChild(element('span', 'stg-mc-cap-label', '模型能力'));
    for (const capability of row.capabilities) {
      const chip = element('span', 'stg-mc-cap', capabilityLabel(capability));
      chip.title = capability;
      capabilities.appendChild(chip);
    }
    detail.appendChild(capabilities);
  }
  const factLines = mediaFactLines(row.mediaProfile);
  if (factLines.length) detail.appendChild(renderMediaFacts(factLines));
  if (row.mediaPricing?.rates.length) detail.appendChild(renderMediaPrices(row));
  // Media generators rarely reason; offer the editor only when the Model
  // declares reasoning support so image/speech rows stay free of noise.
  const reasoningApplies = !media || row.inferenceProfile?.reasoning?.support === 'supported';
  if (reasoningApplies) {
    const boundary = element('p', 'stg-mc-boundary');
    boundary.textContent = t('modelCatalog.aa.modelBoundary');
    detail.appendChild(boundary);
  }
  if (stageReasoning && reasoningApplies) {
    const previous = editors.get(identity);
    const editor = previous?.dataset.profileStamp === JSON.stringify(row.inferenceProfile ?? {})
      ? previous : createReasoningEditor({
    profile: row.inferenceProfile,
    stage: profile => {
      stageReasoning?.(row.creatorId, row.modelId, profile);
      row.inferenceProfile = profile;
      const model = activeDocument?.models.find(m => m.creator_id === row.creatorId && m.model_id === row.modelId);
      if (model) model.inference_profile = profile;
      editor.dataset.profileStamp = JSON.stringify(profile);
    },
    });
    editor.dataset.modelIdentity = identity;
    editor.dataset.profileStamp = JSON.stringify(row.inferenceProfile ?? {});
    detail.appendChild(editor);
  }
  card.append(summary, detail);
  return card;
}

function renderKindBadge(kind: string): HTMLElement {
  const badge = element('span', 'stg-mc-kind-badge');
  badge.dataset.kind = kind;
  badge.innerHTML = kindGlyph(kind);
  badge.appendChild(element('span', undefined, kindLabel(kind)));
  return badge;
}

function renderMediaFacts(lines: ReturnType<typeof mediaFactLines>): HTMLElement {
  const section = element('section', 'stg-mc-media');
  section.appendChild(element('strong', 'stg-mc-media-title', t('modelCatalog.media.specTitle')));
  const facts = element('dl', 'stg-mc-media-facts');
  for (const line of lines) {
    const row = element('div', 'stg-mc-media-fact');
    row.append(element('dt', undefined, line.label), element('dd', undefined, line.value));
    facts.appendChild(row);
  }
  section.appendChild(facts);
  return section;
}

function renderMediaPrices(row: ModelCatalogRow): HTMLElement {
  const pricing = row.mediaPricing!;
  const section = element('section', 'stg-mc-media');
  section.appendChild(element('strong', 'stg-mc-media-title', t('modelCatalog.media.priceTitle')));
  const table = element('table', 'stg-mc-media-prices');
  const body = element('tbody');
  for (const rate of pricing.rates) {
    const line = element('tr');
    line.append(
      element('th', undefined, rate.tier || '—'),
      element('td', undefined, formatMediaRate(rate, (value) => formatPrice(value, pricing.currency))),
    );
    body.appendChild(line);
  }
  table.appendChild(body);
  section.appendChild(table);
  if (pricing.rates.some((rate) => rate.estimated)) {
    section.appendChild(element('p', 'stg-mc-media-note', t('modelCatalog.media.estimatedNote')));
  }
  return section;
}

function renderKindFilter(counts: Map<string, number>): HTMLElement {
  const bar = element('div', 'stg-mc-kind-bar');
  bar.setAttribute('role', 'group');
  bar.setAttribute('aria-label', t('modelCatalog.kindFilter'));
  const chip = (kind: string | null, label: string, count: number) => {
    const active = kindFilter === kind;
    const button = element('button', `stg-mc-kind-chip${active ? ' is-active' : ''}`);
    button.type = 'button';
    button.setAttribute('aria-pressed', String(active));
    if (kind) {
      button.dataset.kind = kind;
      button.innerHTML = kindGlyph(kind);
    }
    button.append(element('span', undefined, label),
      element('span', 'stg-mc-kind-chip-count', String(count)));
    button.addEventListener('click', () => {
      kindFilter = active ? null : kind;
      render();
    });
    return button;
  };
  let total = 0;
  for (const count of counts.values()) total += count;
  bar.appendChild(chip(null, t('modelCatalog.kindAll'), total));
  for (const kind of taxonomy().getModelKinds()) {
    const count = counts.get(kind.id);
    if (count) bar.appendChild(chip(kind.id, kindLabel(kind.id), count));
  }
  return bar;
}

function renderVendorTile(group: VendorGroup, active: boolean): HTMLButtonElement {
  const tile = element('button', `stg-mc-vendor-tile${active ? ' is-active' : ''}`);
  tile.type = 'button';
  tile.setAttribute('aria-pressed', String(active));
  const icon = element('span', 'stg-mc-vendor-tile-icon');
  icon.innerHTML = brandIconHtml(group.icon, 26);
  tile.append(
    icon,
    element('span', 'stg-mc-vendor-tile-label', group.label),
    element('span', 'stg-mc-vendor-tile-count', String(group.models.length)),
  );
  tile.addEventListener('click', () => {
    openVendorId = active ? null : group.vendorId;
    render();
  });
  return tile;
}

function filteredGroups(groups: VendorGroup[]): VendorGroup[] {
  const query = searchQuery.trim().toLowerCase();
  if (!query) return groups;
  return groups.map((group) => ({
    ...group,
    models: group.models.filter((row) => (
      `${row.displayName} ${row.modelId} ${row.creatorId} ${row.creatorLabel}`
        .toLowerCase().includes(query)
    )),
  })).filter((group) => group.models.length > 0);
}

function renderVendorGroup(group: VendorGroup, remainingBudget: number, editors: Map<string, HTMLElement>): HTMLElement {
  const section = element('section', 'stg-mc-vendor');
  const head = element('header', 'stg-mc-vendor-head');
  const icon = element('span', 'stg-mc-vendor-head-icon');
  icon.innerHTML = brandIconHtml(group.icon, 24);
  const heading = element('div');
  heading.append(
    element('strong', undefined, group.label),
    element('span', undefined, `${group.models.length} 个模型 · ${t('modelReasoning.title')}`),
  );
  head.append(icon, heading);
  const list = element('div', 'stg-mc-vendor-models');
  const visible = group.models.slice(0, Math.max(0, remainingBudget));
  for (const row of visible) list.appendChild(renderModel(row, editors));
  if (visible.length < group.models.length) {
    list.appendChild(element('p', 'stg-mc-limit-note',
      `为控制浏览器资源，本次显示前 ${visible.length} 个模型；请使用搜索缩小范围。`));
  }
  section.append(head, list);
  return section;
}

function render(): void {
  const root = container();
  if (!root) return;
  // Preserve live inputs across asynchronous AA repaints. Only currently
  // mounted editors (at most MAX_VISIBLE_MODELS) survive this one render;
  // changed profiles and unmounted Models release their old form nodes.
  const editors = new Map(Array.from(root.querySelectorAll<HTMLElement>(
    '.stg-reasoning-editor[data-model-identity]'), node => [node.dataset.modelIdentity!, node]));
  const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  if (focused && root.contains(focused) && focused.closest('.stg-reasoning-editor')) {
    queueMicrotask(() => { if (root.contains(focused)) focused.focus({ preventScroll: true }); });
  }
  root.replaceChildren();
  if (!activeDocument || activeDocument.contract_version !== 'tofu.model-routing/v2') {
    root.appendChild(element('p', 'stg-mc-state', '正在加载模型目录…'));
    return;
  }
  const allGroups = buildVendorGroups(activeDocument, activeAa?.scores ?? {}, taxonomy().modelKind);
  const kindCounts = countModelKinds(allGroups);
  if (kindFilter && !kindCounts.has(kindFilter)) kindFilter = null;
  const groups = filteredGroups(filterGroupsByKind(allGroups, kindFilter));
  const input = document.getElementById('stgModelCatalogSearch') as HTMLInputElement | null;
  if (input && input.value !== searchQuery) input.value = searchQuery;
  // AA scores and the cost frontier describe conversation models.
  if (!searchQuery && (!kindFilter || kindFilter === 'chat')) {
    root.appendChild(renderAaSource(countAaScoredModels(allGroups)));
    if (allGroups.length) root.appendChild(renderParetoEntry(allGroups));
  }
  if (!allGroups.length) {
    root.appendChild(element('p', 'stg-mc-state', '尚未登记官方模型。'));
    return;
  }
  if (kindCounts.size > 1) root.appendChild(renderKindFilter(kindCounts));

  const count = groups.reduce((total, group) => total + group.models.length, 0);
  root.appendChild(element('div', 'stg-mc-count',
    searchQuery ? `${count} 个匹配模型 · ${groups.length} 个开发厂商` : `${count} 个模型 · ${groups.length} 个开发厂商`));
  if (!groups.length) {
    root.appendChild(element('p', 'stg-mc-state', '没有匹配的模型。'));
    return;
  }

  if (!searchQuery) {
    const grid = element('div', 'stg-mc-vendor-grid');
    for (const group of groups) grid.appendChild(renderVendorTile(group, group.vendorId === openVendorId));
    root.appendChild(grid);
    const openGroup = groups.find((group) => group.vendorId === openVendorId);
    if (openGroup) root.appendChild(renderVendorGroup(openGroup, MAX_VISIBLE_MODELS, editors));
    return;
  }

  let remaining = MAX_VISIBLE_MODELS;
  for (const group of groups) {
    if (remaining <= 0) break;
    root.appendChild(renderVendorGroup(group, remaining, editors));
    remaining -= Math.min(group.models.length, remaining);
  }
}

export function renderModelCatalogPanel(documentValue: ModelCatalogDocument,
  onStageReasoning?: typeof stageReasoning): void {
  stageReasoning = onStageReasoning;
  activeDocument = documentValue;
  render();
  if (!aaLoaded && !aaBusy) void loadAa();
}

export function setModelCatalogSearch(value: unknown): void {
  searchQuery = String(value ?? '');
  render();
}

export function repaintModelCatalogPanel(): void {
  if (activeDocument) render();
}

/** Drop the cached AA block so the next render refetches it once. The
 *  settings live-sync calls this when the routing authority committed a new
 *  revision, since a scheduled intelligence pass may have moved scores. */
export function invalidateModelCatalogAaScores(): void {
  aaLoaded = false;
}

export function destroyModelCatalogPanel(): void {
  closeParetoDialog();
  activeDocument = null;
  stageReasoning = undefined;
  searchQuery = '';
  openVendorId = null;
  kindFilter = null;
  openModels.clear();
  aaRequestGeneration += 1;
  activeAa = null;
  aaLoaded = false;
  aaBusy = false;
  aaGuideOpen = false;
  aaError = '';
  container()?.replaceChildren();
}
