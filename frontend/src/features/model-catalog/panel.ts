/**
 * Read-only Creator/Model Settings catalog for model-routing v2.
 *
 * Entry points: renderModelCatalogPanel, setModelCatalogSearch,
 * repaintModelCatalogPanel, destroyModelCatalogPanel. Dependencies are the v2
 * Creator/Model facts plus shared brand and price presentation. Provider,
 * Offering, Deployment, alias, route, and credential state are prohibited.
 */

import './model-catalog.css';
import { brandIconHtml } from '../../core/model-brand-icons';
import { _i18nLang, t } from '../../i18n';
import { modelPricePresentation } from '../settings/model-price-localization';
import { fetchAaScores, refreshAaScores, saveAaKey } from './api';
import { buildVendorGroups } from './model';
import { buildParetoPoints, closeParetoDialog, openParetoDialog } from './pareto-chart';
import type {
  AaBlock,
  ModelCatalogDocument,
  ModelCatalogRow,
  ModelPricing,
  VendorGroup,
} from './types';

const MAX_VISIBLE_MODELS = 240;
let activeDocument: ModelCatalogDocument | null = null;
let searchQuery = '';
let openVendorId: string | null = null;
const openModels = new Set<string>();
let activeAa: AaBlock | null = null;
let aaLoaded = false;
let aaBusy = false;
let aaGuideOpen = false;
let aaError = '';
let aaRequestGeneration = 0;

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

function formatPricing(pricing: ModelPricing | null): string {
  if (!pricing) return '未登记';
  const input = pricing.input == null ? '—' : formatPrice(pricing.input, pricing.currency);
  const output = pricing.output == null ? '—' : formatPrice(pricing.output, pricing.currency);
  return `输入 ${input} · 输出 ${output}`;
}

function formatContextWindow(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '未登记';
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
    + '<span class="stg-mc-pareto-entry-text"><strong>成本 × AA Index</strong>'
    + `<span>${points.length >= 2
      ? `${points.length} 个模型 · 官方价格 · Pareto 前沿`
      : t('modelCatalog.aa.chartWaiting')}</span></span>`
    + '<svg class="stg-mc-pareto-entry-chevron" viewBox="0 0 24 24" aria-hidden="true">'
    + '<path d="M9 5l7 7-7 7"/></svg>';
  if (points.length >= 2) {
    button.addEventListener('click', () => openParetoDialog(groups, { toUsd: priceToUsd }));
  }
  return button;
}

function aaScoreCount(): number {
  return Object.keys(activeAa?.scores ?? {}).length;
}

function aaStatusCopy(): { label: string; detail: string; state: string } {
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
      detail: t('modelCatalog.aa.readyDetail', { n: aaScoreCount() }),
      state: 'ready',
    };
  }
  if (activeAa?.status === 'stale') {
    return {
      label: t('modelCatalog.aa.statusStale'),
      detail: t('modelCatalog.aa.staleDetail', { n: aaScoreCount() }),
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

function aaKeySourceLabel(): string {
  if (activeAa?.key_source === 'settings') {
    return t('modelCatalog.aa.keySourceSettings', { hint: activeAa.key_hint || '••••' });
  }
  if (activeAa?.key_source === 'legacy_config') {
    return t('modelCatalog.aa.keySourceLegacy', { hint: activeAa.key_hint || '••••' });
  }
  if (activeAa?.key_source === 'env') {
    return t('modelCatalog.aa.keySourceEnv', { hint: activeAa.key_hint || '••••' });
  }
  return t('modelCatalog.aa.keySourceNone');
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

function renderAaSource(): HTMLElement {
  const status = aaStatusCopy();
  const disclosure = element('details', `stg-mc-aa-source is-${status.state}`);
  disclosure.open = aaGuideOpen || activeAa?.status === 'no_key';
  disclosure.addEventListener('toggle', () => { aaGuideOpen = disclosure.open; });

  const summary = element('summary', 'stg-mc-aa-summary');
  summary.appendChild(element('span', 'stg-mc-aa-mark', 'AA'));
  const identity = element('span', 'stg-mc-aa-identity');
  identity.append(
    element('strong', undefined, t('modelCatalog.aa.sourceTitle')),
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
  controls.appendChild(element('span', 'stg-mc-aa-key-source', aaKeySourceLabel()));
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
  body.append(explanation, form, controls);
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

function renderModel(row: ModelCatalogRow): HTMLElement {
  const identity = modelIdentity(row);
  const card = element('details', 'stg-mc-model');
  card.open = openModels.has(identity);
  card.addEventListener('toggle', () => {
    if (card.open) openModels.add(identity);
    else openModels.delete(identity);
  });

  const summary = element('summary', 'stg-mc-model-head');
  const title = element('div', 'stg-mc-model-title');
  title.innerHTML = brandIconHtml(row.brand, 21);
  const titleText = element('div', 'stg-mc-model-title-text');
  titleText.append(
    element('strong', undefined, row.displayName),
    element('span', 'stg-mc-model-id', identity),
  );
  title.appendChild(titleText);
  const brief = element('div', 'stg-mc-model-brief');
  const intelligence = row.aa?.intelligence;
  if (intelligence !== null && intelligence !== undefined) {
    const score = element('span', 'stg-mc-score', `AA ${Number(intelligence.toFixed(1))}`);
    score.title = t('modelCatalog.aa.scoreMatched', {
      score: Number(intelligence.toFixed(1)),
      name: row.aa?.aa_name || row.displayName,
    });
    brief.appendChild(score);
  } else {
    brief.appendChild(element('span', 'stg-mc-score na', 'AA —'));
  }
  brief.appendChild(element('span', 'stg-mc-brief-price', formatPricing(row.pricing)));
  const caret = element('span', 'stg-mc-caret');
  caret.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5"/></svg>';
  summary.append(title, brief, caret);

  const detail = element('div', 'stg-mc-model-detail');
  const facts = element('div', 'stg-mc-model-facts');
  facts.append(
    renderMetric('AA Intelligence Index', intelligence === null || intelligence === undefined
      ? t('modelCatalog.aa.unscored') : String(Number(intelligence.toFixed(1))), 'quality'),
    renderMetric('官方价格 / 1M', formatPricing(row.pricing), 'price'),
    renderMetric('上下文', formatContextWindow(row.contextWindow)),
    renderMetric('发布时间', row.releaseDate || '未登记'),
    renderMetric('生命周期', row.lifecycle || 'stable'),
  );
  detail.appendChild(facts);
  if (row.aa) {
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
      capabilities.appendChild(element('span', 'stg-mc-cap', capability));
    }
    detail.appendChild(capabilities);
  }
  const boundary = element('p', 'stg-mc-boundary');
  boundary.textContent = t('modelCatalog.aa.modelBoundary');
  detail.appendChild(boundary);
  card.append(summary, detail);
  return card;
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

function renderVendorGroup(group: VendorGroup, remainingBudget: number): HTMLElement {
  const section = element('section', 'stg-mc-vendor');
  const head = element('header', 'stg-mc-vendor-head');
  const icon = element('span', 'stg-mc-vendor-head-icon');
  icon.innerHTML = brandIconHtml(group.icon, 24);
  const heading = element('div');
  heading.append(
    element('strong', undefined, group.label),
    element('span', undefined, `${group.models.length} 个模型 · 官方模型只读`),
  );
  head.append(icon, heading);
  const list = element('div', 'stg-mc-vendor-models');
  const visible = group.models.slice(0, Math.max(0, remainingBudget));
  for (const row of visible) list.appendChild(renderModel(row));
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
  root.replaceChildren();
  if (!activeDocument || activeDocument.contract_version !== 'tofu.model-routing/v2') {
    root.appendChild(element('p', 'stg-mc-state', '正在加载模型目录…'));
    return;
  }
  const allGroups = buildVendorGroups(activeDocument, activeAa?.scores ?? {});
  const groups = filteredGroups(allGroups);
  const input = document.getElementById('stgModelCatalogSearch') as HTMLInputElement | null;
  if (input && input.value !== searchQuery) input.value = searchQuery;
  if (!searchQuery) {
    root.appendChild(renderAaSource());
    if (allGroups.length) root.appendChild(renderParetoEntry(allGroups));
  }
  if (!allGroups.length) {
    root.appendChild(element('p', 'stg-mc-state', '尚未登记官方模型。'));
    return;
  }

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
    if (openGroup) root.appendChild(renderVendorGroup(openGroup, MAX_VISIBLE_MODELS));
    return;
  }

  let remaining = MAX_VISIBLE_MODELS;
  for (const group of groups) {
    if (remaining <= 0) break;
    root.appendChild(renderVendorGroup(group, remaining));
    remaining -= Math.min(group.models.length, remaining);
  }
}

export function renderModelCatalogPanel(documentValue: ModelCatalogDocument): void {
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

export function destroyModelCatalogPanel(): void {
  closeParetoDialog();
  activeDocument = null;
  searchQuery = '';
  openVendorId = null;
  openModels.clear();
  aaRequestGeneration += 1;
  activeAa = null;
  aaLoaded = false;
  aaBusy = false;
  aaGuideOpen = false;
  aaError = '';
  container()?.replaceChildren();
}
