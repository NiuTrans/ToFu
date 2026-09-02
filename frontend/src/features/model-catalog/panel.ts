import './model-catalog.css';
import { featureRegistry } from '../../feature-registry';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import { _i18nLang, type I18nKey } from '../../i18n';
import { modelPricePresentation } from '../settings/model-price-localization';
import {
  fetchModelCatalog,
  isModelCatalogConflict,
  putModelCatalog,
} from './api';
import {
  addModelWithOffering,
  attachOffering,
  buildLogicalRows,
  cloneCatalog,
  hasOffering,
  removeOffering,
  setLogicalEnabled,
  setOfferingEnabled,
  updateOfferingConfiguration,
} from './model';
import { MODEL_CATALOG_CONTRACT_VERSION } from './types';
import type {
  HealthMap,
  LogicalRow,
  ModelCatalogEnvelope,
  Offering,
  OfferingConfiguration,
  OfferingRow,
  Pricing,
  ProviderMap,
} from './types';

type PanelTranslator = (
  key: I18nKey | string,
  params?: Record<string, unknown>,
) => string;

type FormMode = 'add' | 'attach' | 'edit';

interface FormState {
  mode: FormMode;
  offeringId: string;
  values: Record<string, string>;
}

const FALLBACK_LABELS: Record<string, string> = {
  'modelCatalog.loading': 'Loading model catalog…',
  'modelCatalog.empty': 'No models found in the catalog.',
  'modelCatalog.error': 'Failed to load the model catalog.',
  'modelCatalog.conflict': 'The catalog changed on the server. Showing the latest revision.',
  'modelCatalog.retry': 'Retry',
  'modelCatalog.refresh': 'Refresh',
  'modelCatalog.addModel': 'Add model',
  'modelCatalog.attachOffering': 'Attach offering',
  'modelCatalog.editOffering': 'Edit offering',
  'modelCatalog.removeOffering': 'Remove offering',
  'modelCatalog.expand': 'Show offerings',
  'modelCatalog.collapse': 'Hide offerings',
  'modelCatalog.enabled': 'Enabled',
  'modelCatalog.capabilities': 'Capabilities',
  'modelCatalog.providers': 'Providers',
  'modelCatalog.offeringCount': '{n} offerings',
  'modelCatalog.pricing': 'Pricing',
  'modelCatalog.context': 'Context',
  'modelCatalog.healthy': 'Healthy',
  'modelCatalog.unhealthy': 'Unhealthy',
  'modelCatalog.rpm': 'RPM',
  'modelCatalog.wireIds': 'Wire IDs',
  'modelCatalog.protocol': 'Protocol',
  'modelCatalog.modelId': 'Model ID',
  'modelCatalog.displayName': 'Display name',
  'modelCatalog.provider': 'Provider',
  'modelCatalog.requestIds': 'Request IDs',
  'modelCatalog.contextWindow': 'Context window',
  'modelCatalog.inputPrice': 'Input',
  'modelCatalog.outputPrice': 'Output',
  'modelCatalog.currency': 'Currency',
  'modelCatalog.save': 'Save',
  'modelCatalog.cancel': 'Cancel',
  'modelCatalog.formAddTitle': 'Add a logical model',
  'modelCatalog.formAttachTitle': 'Attach a provider offering',
  'modelCatalog.formEditTitle': 'Edit provider offering',
  'modelCatalog.duplicateOffering': 'This provider already offers this model.',
  'modelCatalog.modelExists': 'This logical model already exists — attach another offering instead.',
  'modelCatalog.noProviders': 'No provider accounts are configured. Add one on the Providers tab first.',
  'modelCatalog.removeConfirm': 'Remove offering {offering}?',
  'modelCatalog.validationError': 'Fix the highlighted fields and try again.',
  'modelCatalog.updated': 'Catalog saved.',
};

let scope: LifecycleScope | null = null;
let envelope: ModelCatalogEnvelope | null = null;
let generation = 0;
let busy = false;
let statusMessage: string | null = null;
let formState: FormState | null = null;
const expandedIds = new Set<string>();
let offeringSectionSeq = 0;

function translator(): PanelTranslator {
  const candidate = (featureRegistry as unknown as { t?: unknown }).t;
  if (typeof candidate !== 'function') {
    return (key) => String(key);
  }
  return candidate as PanelTranslator;
}

function translate(
  key: I18nKey | string,
  params?: Record<string, unknown>,
): string {
  const value = translator()(key, params);
  if (value && value !== key) return value;
  return FALLBACK_LABELS[String(key)] ?? String(key);
}

function container(): HTMLElement | null {
  return document.getElementById('stgModelCatalogList');
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

function envelopeRevision(value: ModelCatalogEnvelope): number | null {
  const revision = value.revision ?? value.catalog?.revision;
  return typeof revision === 'number'
    && Number.isSafeInteger(revision)
    && revision >= 0
    ? revision : null;
}

function hasCurrentContract(
  value: ModelCatalogEnvelope | null | undefined,
): value is ModelCatalogEnvelope {
  return value?.contract_version === MODEL_CATALOG_CONTRACT_VERSION
    && value.catalog?.contract_version === MODEL_CATALOG_CONTRACT_VERSION;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return String(error ?? '');
}

function priceRates(): Record<string, unknown> {
  const scope = (typeof window !== 'undefined' ? window : {}) as Record<string, unknown>;
  const policy = scope._modelPriceDisplayPolicy as { usd_rates?: unknown } | undefined;
  const rates = policy?.usd_rates;
  return rates && typeof rates === 'object'
    ? rates as Record<string, unknown> : { USD: 1 };
}

function formatPrice(value: unknown, currency: unknown): string {
  const number = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(number) || number < 0) return '—';
  return modelPricePresentation.formatForUi(
    number, currency ?? 'USD', _i18nLang, priceRates());
}

function formatPricing(pricing: Pricing | null): string {
  if (!pricing) return '—';
  const parts: string[] = [];
  if (pricing.input != null) {
    parts.push(`${translate('modelCatalog.inputPrice')} ${formatPrice(pricing.input, pricing.currency)}`);
  }
  if (pricing.output != null) {
    parts.push(`${translate('modelCatalog.outputPrice')} ${formatPrice(pricing.output, pricing.currency)}`);
  }
  return parts.length ? parts.join(' · ') : '—';
}

function formatContextWindow(value: number | null): string {
  if (value == null || value <= 0) return '—';
  if (value >= 1_000_000) return `${Math.round((value / 1_000_000) * 100) / 100}M`;
  if (value >= 1000) return `${Math.round(value / 1000)}K`;
  return String(Math.round(value));
}

function parseList(value: string): string[] {
  const parts = value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
  return [...new Set(parts)];
}

function renderStateElement(kind: 'loading' | 'empty' | 'error' | 'conflict', detail?: string): HTMLElement {
  const state = element('div', `stg-mc-state stg-mc-${kind}`);
  state.setAttribute('role', kind === 'empty' ? 'status' : 'alert');
  let text = translate(
    kind === 'loading' ? 'modelCatalog.loading'
      : kind === 'empty' ? 'modelCatalog.empty'
        : kind === 'error' ? 'modelCatalog.error'
          : 'modelCatalog.conflict',
  );
  if (kind === 'error' && detail) text = `${text} ${detail}`;
  state.appendChild(element('span', 'stg-mc-state-text', text));
  if (kind === 'error' || kind === 'conflict') {
    const retry = element('button', 'stg-mc-retry', translate('modelCatalog.retry'));
    retry.type = 'button';
    retry.dataset.mcRetry = '1';
    retry.addEventListener('click', () => { void refresh(); });
    state.appendChild(retry);
  }
  return state;
}

function renderToolbar(): HTMLElement {
  const toolbar = element('div', 'stg-mc-toolbar');
  const add = element('button', 'stg-mc-btn stg-mc-primary', translate('modelCatalog.addModel'));
  add.type = 'button';
  add.dataset.mcAdd = '1';
  add.disabled = busy || !envelope;
  toolbar.appendChild(add);

  const refreshBtn = element('button', 'stg-mc-btn', translate('modelCatalog.refresh'));
  refreshBtn.type = 'button';
  refreshBtn.dataset.mcRefresh = '1';
  refreshBtn.disabled = busy;
  refreshBtn.addEventListener('click', () => { void refresh(); });
  toolbar.appendChild(refreshBtn);
  return toolbar;
}

function providerOptions(providers: ProviderMap | undefined, selected: string): HTMLOptionElement[] {
  const entries = Object.entries(providers ?? {});
  entries.sort((left, right) => {
    const leftLabel = providerDisplayLabel(left[1] as Record<string, unknown>, left[0]);
    const rightLabel = providerDisplayLabel(right[1] as Record<string, unknown>, right[0]);
    return leftLabel < rightLabel ? -1 : leftLabel > rightLabel ? 1 : 0;
  });
  const options: HTMLOptionElement[] = [];
  const placeholder = element('option', '', translate('modelCatalog.provider'));
  placeholder.value = '';
  placeholder.disabled = true;
  options.push(placeholder);
  for (const [id, provider] of entries) {
    const option = element('option', '', providerDisplayLabel(provider as Record<string, unknown>, id));
    option.value = id;
    option.selected = id === selected;
    options.push(option);
  }
  return options;
}

function providerDisplayLabel(provider: Record<string, unknown> | undefined, id: string): string {
  if (!provider) return id;
  if (typeof provider.label === 'string' && provider.label.trim()) return provider.label;
  if (typeof provider.name === 'string' && provider.name.trim()) return provider.name;
  if (typeof provider.brand === 'string' && provider.brand.trim()) return provider.brand;
  return id;
}

function fieldRow(labelKey: string, control: HTMLElement, hint?: string): HTMLElement {
  const row = element('div', 'stg-mc-field');
  const label = element('label', 'stg-mc-field-label', translate(labelKey));
  row.appendChild(label);
  row.appendChild(control);
  if (hint) row.appendChild(element('span', 'stg-mc-field-hint', hint));
  return row;
}

function textInput(field: string, value: string, placeholder = ''): HTMLInputElement {
  const input = element('input');
  input.type = 'text';
  input.value = value;
  input.placeholder = placeholder;
  input.dataset.mcField = field;
  input.spellcheck = false;
  return input;
}

function numberInput(field: string, value: string, placeholder = ''): HTMLInputElement {
  const input = element('input');
  input.type = 'number';
  input.value = value;
  input.placeholder = placeholder;
  input.dataset.mcField = field;
  input.min = '0';
  input.step = '1';
  return input;
}

function selectInput(field: string, options: HTMLOptionElement[]): HTMLSelectElement {
  const select = element('select');
  select.dataset.mcField = field;
  for (const option of options) select.appendChild(option);
  return select;
}

function renderForm(): HTMLElement | null {
  if (!formState) return null;
  const state = formState;
  const values = state.values;

  const form = element('form', 'stg-mc-form');
  form.setAttribute('role', 'form');

  const title = element('div', 'stg-mc-form-title', translate(
    state.mode === 'add' ? 'modelCatalog.formAddTitle'
      : state.mode === 'attach' ? 'modelCatalog.formAttachTitle'
        : 'modelCatalog.formEditTitle',
  ));
  form.appendChild(title);

  if (state.mode === 'add') {
    form.appendChild(fieldRow('modelCatalog.modelId', textInput('model_id', values.model_id ?? '', 'gpt-4o')));
    form.appendChild(fieldRow('modelCatalog.displayName', textInput('display_name', values.display_name ?? '')));
  } else {
    const identity = element('div', 'stg-mc-form-identity');
    identity.appendChild(element('span', 'stg-mc-form-identity-label', translate('modelCatalog.modelId')));
    identity.appendChild(element('code', 'stg-mc-form-identity-value', values.model_id ?? state.offeringId));
    form.appendChild(identity);
  }

  if (state.mode === 'edit') {
    const offering = envelope?.catalog?.offerings?.[state.offeringId] as Offering | undefined;
    const providerLabel = providerDisplayLabel(
      envelope?.providers?.[offering?.provider_id ?? ''] as Record<string, unknown> | undefined,
      offering?.provider_id ?? '',
    );
    const identity = element('div', 'stg-mc-form-identity');
    identity.appendChild(element('span', 'stg-mc-form-identity-label', translate('modelCatalog.provider')));
    identity.appendChild(element('code', 'stg-mc-form-identity-value', providerLabel));
    form.appendChild(identity);
  } else {
    const select = selectInput('provider_id', providerOptions(envelope?.providers, values.provider_id ?? ''));
    form.appendChild(fieldRow('modelCatalog.provider', select));
  }

  form.appendChild(fieldRow(
    'modelCatalog.requestIds',
    textInput('request_ids', values.request_ids ?? '', 'wire-a, wire-b'),
  ));
  form.appendChild(fieldRow(
    'modelCatalog.rpm',
    numberInput('rpm', values.rpm ?? ''),
  ));
  form.appendChild(fieldRow(
    'modelCatalog.capabilities',
    textInput('capabilities', values.capabilities ?? '', 'text, thinking'),
  ));
  form.appendChild(fieldRow(
    'modelCatalog.contextWindow',
    numberInput('context_window', values.context_window ?? '', '128000'),
  ));

  const pricing = element('div', 'stg-mc-pricing');
  pricing.appendChild(element('div', 'stg-mc-form-title-sm', translate('modelCatalog.pricing')));
  pricing.appendChild(fieldRow('modelCatalog.inputPrice', numberInput('pricing_input', values.pricing_input ?? '', '1.0')));
  pricing.appendChild(fieldRow('modelCatalog.outputPrice', numberInput('pricing_output', values.pricing_output ?? '', '4.0')));
  const currencySelect = element('select');
  currencySelect.dataset.mcField = 'pricing_currency';
  for (const code of ['USD', 'CNY']) {
    const option = element('option', '', code);
    option.value = code;
    option.selected = (values.pricing_currency ?? 'USD') === code;
    currencySelect.appendChild(option);
  }
  pricing.appendChild(fieldRow('modelCatalog.currency', currencySelect));
  form.appendChild(pricing);

  const actions = element('div', 'stg-mc-form-actions');
  const save = element('button', 'stg-mc-btn stg-mc-primary', translate('modelCatalog.save'));
  save.type = 'button';
  save.dataset.mcSubmit = '1';
  save.disabled = busy;
  actions.appendChild(save);
  const cancel = element('button', 'stg-mc-btn', translate('modelCatalog.cancel'));
  cancel.type = 'button';
  cancel.dataset.mcCancel = '1';
  cancel.disabled = busy;
  actions.appendChild(cancel);
  form.appendChild(actions);

  return form;
}

function renderStatus(): HTMLElement {
  const status = element('div', 'stg-mc-status');
  status.setAttribute('role', 'alert');
  status.textContent = statusMessage ?? '';
  return status;
}

function renderOfferingRow(offering: OfferingRow): HTMLElement {
  const row = element('div', 'stg-mc-offering');
  row.dataset.offeringId = offering.id;
  row.setAttribute('role', 'listitem');

  const toggle = element('label', 'stg-mc-toggle');
  const checkbox = element('input');
  checkbox.type = 'checkbox';
  checkbox.checked = offering.enabled;
  checkbox.dataset.mcToggle = 'offering';
  checkbox.dataset.mcToggleId = offering.id;
  checkbox.setAttribute('aria-label', `${translate('modelCatalog.enabled')} ${offering.id}`);
  toggle.appendChild(checkbox);
  toggle.appendChild(element('span', 'stg-mc-toggle-track'));
  row.appendChild(toggle);

  const provider = element('span', 'stg-mc-offering-provider', offering.providerLabel || offering.providerId || '—');
  row.appendChild(provider);

  const protocol = element('span', `stg-mc-badge stg-mc-protocol ${offering.protocol || 'none'}`, offering.protocol || '—');
  row.appendChild(protocol);

  const wire = element('span', 'stg-mc-offering-wire', offering.wireIds.join(', ') || '—');
  wire.title = offering.wireIds.join(', ');
  row.appendChild(wire);

  const rpm = element('span', 'stg-mc-offering-rpm', offering.rpm != null ? String(offering.rpm) : '—');
  row.appendChild(rpm);

  const pricing = element('span', 'stg-mc-offering-pricing', formatPricing(offering.pricing));
  pricing.title = `${translate('modelCatalog.pricing')}: ${formatPricing(offering.pricing)}`;
  row.appendChild(pricing);

  const context = element('span', 'stg-mc-offering-context', formatContextWindow(offering.contextWindow));
  context.title = `${translate('modelCatalog.context')}: ${formatContextWindow(offering.contextWindow)}`;
  row.appendChild(context);

  const health = element(
    'span',
    `stg-mc-offering-health ${offering.healthy ? 'healthy' : 'unhealthy'}`,
    translate(offering.healthy ? 'modelCatalog.healthy' : 'modelCatalog.unhealthy'),
  );
  row.appendChild(health);

  const actions = element('span', 'stg-mc-offering-actions');
  const edit = element('button', 'stg-mc-link', translate('modelCatalog.editOffering'));
  edit.type = 'button';
  edit.dataset.mcEdit = offering.id;
  edit.disabled = busy;
  actions.appendChild(edit);
  const remove = element('button', 'stg-mc-link stg-mc-danger', translate('modelCatalog.removeOffering'));
  remove.type = 'button';
  remove.dataset.mcRemove = offering.id;
  remove.disabled = busy;
  actions.appendChild(remove);
  row.appendChild(actions);

  return row;
}

function renderModelRow(row: LogicalRow): HTMLElement {
  const section = element('section', 'stg-mc-model');
  section.dataset.modelId = row.id;
  section.setAttribute('role', 'listitem');

  const head = element('div', 'stg-mc-model-head');
  const offeringsId = `stg-mc-offerings-${offeringSectionSeq += 1}`;
  const expanded = expandedIds.has(row.id);

  const expand = element('button', 'stg-mc-expand');
  expand.type = 'button';
  expand.textContent = expanded ? '▾' : '▸';
  expand.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  expand.setAttribute('aria-controls', offeringsId);
  expand.setAttribute('aria-label', translate(expanded ? 'modelCatalog.collapse' : 'modelCatalog.expand'));
  expand.dataset.mcExpand = row.id;
  head.appendChild(expand);

  const toggle = element('label', 'stg-mc-toggle');
  const checkbox = element('input');
  checkbox.type = 'checkbox';
  checkbox.checked = row.enabled;
  checkbox.dataset.mcToggle = 'logical';
  checkbox.dataset.mcToggleId = row.id;
  checkbox.setAttribute('aria-label', `${translate('modelCatalog.enabled')} ${row.label}`);
  toggle.appendChild(checkbox);
  toggle.appendChild(element('span', 'stg-mc-toggle-track'));
  head.appendChild(toggle);

  const label = element('span', 'stg-mc-model-label', row.label);
  head.appendChild(label);
  head.appendChild(element('code', 'stg-mc-model-id', row.id));

  const metaParts: string[] = [];
  if (row.capabilities.length) {
    metaParts.push(`${translate('modelCatalog.capabilities')}: ${row.capabilities.join(', ')}`);
  }
  if (row.providerLabels.length) {
    metaParts.push(`${translate('modelCatalog.providers')}: ${row.providerLabels.join(', ')}`);
  }
  metaParts.push(translate('modelCatalog.offeringCount', { n: row.offerings.length }));
  head.appendChild(element('span', 'stg-mc-model-meta', metaParts.join(' · ')));

  const attach = element('button', 'stg-mc-link', translate('modelCatalog.attachOffering'));
  attach.type = 'button';
  attach.dataset.mcAttach = row.id;
  attach.disabled = busy;
  head.appendChild(attach);

  section.appendChild(head);

  const offerings = element('div', 'stg-mc-offerings');
  offerings.id = offeringsId;
  offerings.hidden = !expanded;
  for (const offering of row.offerings) {
    offerings.appendChild(renderOfferingRow(offering));
  }
  section.appendChild(offerings);

  return section;
}

function renderModelList(rows: LogicalRow[]): HTMLElement {
  const list = element('div', 'stg-mc-list');
  list.setAttribute('role', 'list');
  for (const row of rows) list.appendChild(renderModelRow(row));
  return list;
}

function render(): void {
  const root = container();
  if (!root) return;
  offeringSectionSeq = 0;
  root.replaceChildren();
  root.appendChild(renderToolbar());
  if (formState) {
    const form = renderForm();
    if (form) root.appendChild(form);
  }
  if (!envelope) {
    root.appendChild(renderStateElement(statusMessage ? 'error' : 'loading', statusMessage ?? undefined));
    return;
  }
  if (statusMessage) root.appendChild(renderStatus());
  const rows = buildLogicalRows(envelope.catalog, envelope.providers, envelope.health);
  if (!rows.length) root.appendChild(renderStateElement('empty'));
  else root.appendChild(renderModelList(rows));
}

function onExpandClick(target: HTMLElement): void {
  const id = target.dataset.mcExpand;
  if (!id) return;
  const section = target.closest<HTMLElement>('.stg-mc-model');
  const offerings = section?.querySelector<HTMLElement>('.stg-mc-offerings');
  if (!offerings) return;
  const expanded = !expandedIds.has(id);
  if (expanded) expandedIds.add(id);
  else expandedIds.delete(id);
  offerings.hidden = !expanded;
  target.setAttribute('aria-expanded', expanded ? 'true' : 'false');
  target.setAttribute('aria-label', translate(expanded ? 'modelCatalog.collapse' : 'modelCatalog.expand'));
  target.textContent = expanded ? '▾' : '▸';
}

function formFieldKey(field: string): string {
  return field;
}

function syncFormField(target: HTMLInputElement | HTMLSelectElement): void {
  if (!formState) return;
  const field = target.dataset.mcField;
  if (!field) return;
  formState.values[formFieldKey(field)] = target.value;
}

function newFormState(mode: FormMode, values: Record<string, string>, offeringId = ''): FormState {
  return { mode, offeringId, values };
}

function offeringFromCatalog(offeringId: string): Offering | undefined {
  return envelope?.catalog?.offerings?.[offeringId] as Offering | undefined;
}

function openAddForm(): void {
  formState = newFormState('add', {
    model_id: '',
    display_name: '',
    provider_id: '',
    request_ids: '',
    rpm: '',
    capabilities: '',
    context_window: '',
    pricing_input: '',
    pricing_output: '',
    pricing_currency: 'USD',
  });
  statusMessage = null;
  render();
}

function openAttachForm(modelId: string): void {
  formState = newFormState('attach', {
    model_id: modelId,
    provider_id: '',
    request_ids: '',
    rpm: '',
    capabilities: '',
    context_window: '',
    pricing_input: '',
    pricing_output: '',
    pricing_currency: 'USD',
  });
  statusMessage = null;
  render();
}

function openEditForm(offeringId: string): void {
  const offering = offeringFromCatalog(offeringId);
  if (!offering) return;
  const config = offering.configuration ?? {};
  const pricing = config.pricing as Pricing | null | undefined;
  formState = newFormState('edit', {
    model_id: offering.model_id ?? '',
    offering_id: offeringId,
    request_ids: Array.isArray(config.request_ids) ? config.request_ids.join(', ') : '',
    rpm: config.rpm != null ? String(config.rpm) : '',
    capabilities: Array.isArray(config.capabilities) ? config.capabilities.join(', ') : '',
    context_window: config.context_window != null ? String(config.context_window) : '',
    pricing_input: pricing?.input != null ? String(pricing.input) : '',
    pricing_output: pricing?.output != null ? String(pricing.output) : '',
    pricing_currency: pricing?.currency ? String(pricing.currency) : 'USD',
  }, offeringId);
  statusMessage = null;
  render();
}

function parsePositiveIntOrNull(raw: string): number | null | 'invalid' {
  const value = raw.trim();
  if (!value) return null;
  const number = Number(value);
  if (!Number.isInteger(number) || number <= 0) return 'invalid';
  return number;
}

function parseNonNegativeNumber(raw: string): number | null | 'invalid' {
  const value = raw.trim();
  if (!value) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 'invalid';
  return number;
}

interface ParsedForm {
  mode: FormMode;
  modelId: string;
  displayName: string;
  providerId: string;
  offeringId: string;
  configuration: OfferingConfiguration;
}

function parseForm(state: FormState): { ok: true; parsed: ParsedForm } | { ok: false; errors: string[] } {
  const values = state.values;
  const errors: string[] = [];
  const modelId = (values.model_id ?? '').trim();
  const providerId = (values.provider_id ?? '').trim();
  const offeringId = state.offeringId;

  if (state.mode === 'add' && !modelId) {
    errors.push(`${translate('modelCatalog.modelId')}: required`);
  }
  if (state.mode !== 'edit' && !providerId) {
    errors.push(`${translate('modelCatalog.provider')}: required`);
  }

  const rpm = parsePositiveIntOrNull(values.rpm ?? '');
  if (rpm === 'invalid') errors.push(`${translate('modelCatalog.rpm')}: positive integer`);

  const contextWindow = parsePositiveIntOrNull(values.context_window ?? '');
  if (contextWindow === 'invalid') errors.push(`${translate('modelCatalog.contextWindow')}: positive integer`);

  const capabilities = parseList(values.capabilities ?? '');
  const requestIds = parseList(values.request_ids ?? '');

  const pricingInput = parseNonNegativeNumber(values.pricing_input ?? '');
  const pricingOutput = parseNonNegativeNumber(values.pricing_output ?? '');
  if (pricingInput === 'invalid' || pricingOutput === 'invalid') {
    errors.push(`${translate('modelCatalog.pricing')}: non-negative numbers`);
  }
  const hasInput = pricingInput !== null && pricingInput !== 'invalid';
  const hasOutput = pricingOutput !== null && pricingOutput !== 'invalid';
  if (hasInput !== hasOutput) {
    errors.push(`${translate('modelCatalog.pricing')}: input and output must be set together`);
  }

  if (errors.length) return { ok: false, errors };

  const configuration: OfferingConfiguration = { capabilities };
  if (requestIds.length) configuration.request_ids = requestIds;
  if (rpm !== null && rpm !== 'invalid') configuration.rpm = rpm;
  if (contextWindow !== null && contextWindow !== 'invalid') {
    configuration.context_window = contextWindow;
  }
  if (hasInput && hasOutput) {
    configuration.pricing = {
      input: pricingInput as number,
      output: pricingOutput as number,
      currency: (values.pricing_currency ?? 'USD').toUpperCase() || 'USD',
      unit: 'per_million_tokens',
    };
  }

  return {
    ok: true,
    parsed: {
      mode: state.mode,
      modelId,
      displayName: (values.display_name ?? '').trim(),
      providerId,
      offeringId,
      configuration,
    },
  };
}

function renderFormErrors(errors: string[]): void {
  const root = container();
  if (!root) return;
  const alert = element('div', 'stg-mc-validation');
  alert.setAttribute('role', 'alert');
  alert.appendChild(element('div', 'stg-mc-validation-title', translate('modelCatalog.validationError')));
  const list = element('ul', 'stg-mc-validation-list');
  for (const error of errors) list.appendChild(element('li', '', error));
  alert.appendChild(list);
  const form = root.querySelector('.stg-mc-form');
  if (form?.parentNode) form.parentNode.insertBefore(alert, form.nextSibling);
}

async function applyMutation(build: (catalog: ReturnType<typeof cloneCatalog>) => void): Promise<void> {
  const snapshot = envelope;
  if (!snapshot) return;
  const expectedRevision = envelopeRevision(snapshot);
  if (!hasCurrentContract(snapshot) || expectedRevision === null) {
    statusMessage = translate('modelCatalog.error');
    render();
    return;
  }
  const catalog = cloneCatalog(snapshot.catalog);
  build(catalog);
  busy = true;
  render();
  try {
    const next = await putModelCatalog(expectedRevision, catalog);
    if (next && next.ok !== false && hasCurrentContract(next)) {
      envelope = next;
      formState = null;
      statusMessage = translate('modelCatalog.updated');
      render();
    } else {
      statusMessage = next?.error || translate('modelCatalog.error');
      render();
    }
  } catch (error: unknown) {
    if (isModelCatalogConflict(error)) {
      const refreshed = await refresh();
      if (refreshed) statusMessage = translate('modelCatalog.conflict');
      return;
    }
    statusMessage = errorMessage(error);
    render();
  } finally {
    busy = false;
    render();
  }
}

function submitForm(): void {
  if (!formState || busy) return;
  const parsed = parseForm(formState);
  if (!parsed.ok) {
    renderFormErrors(parsed.errors);
    return;
  }
  const { mode, modelId, displayName, providerId, offeringId, configuration } = parsed.parsed;
  if (!envelope) return;

  if (mode === 'add') {
    if (envelope.catalog.models[modelId]) {
      renderFormErrors([translate('modelCatalog.modelExists')]);
      return;
    }
    if (hasOffering(envelope.catalog, providerId, modelId)) {
      renderFormErrors([translate('modelCatalog.duplicateOffering')]);
      return;
    }
    void applyMutation((catalog) => {
      addModelWithOffering(catalog, { modelId, displayName, providerId, configuration });
    });
    return;
  }

  if (mode === 'attach') {
    if (hasOffering(envelope.catalog, providerId, modelId)) {
      renderFormErrors([translate('modelCatalog.duplicateOffering')]);
      return;
    }
    void applyMutation((catalog) => {
      attachOffering(catalog, { modelId, providerId, configuration });
    });
    return;
  }

  // edit
  if (!offeringFromCatalog(offeringId)) {
    renderFormErrors([translate('modelCatalog.error')]);
    return;
  }
  void applyMutation((catalog) => {
    updateOfferingConfiguration(catalog, offeringId, configuration);
  });
}

async function applyToggle(kind: 'logical' | 'offering', id: string, enabled: boolean): Promise<void> {
  const snapshot = envelope;
  if (!snapshot) return;
  const expectedRevision = envelopeRevision(snapshot);
  if (!hasCurrentContract(snapshot) || expectedRevision === null) {
    statusMessage = translate('modelCatalog.error');
    render();
    return;
  }
  const catalog = cloneCatalog(snapshot.catalog);
  if (kind === 'logical') setLogicalEnabled(catalog, id, enabled);
  else setOfferingEnabled(catalog, id, enabled);
  busy = true;
  render();
  try {
    const next = await putModelCatalog(expectedRevision, catalog);
    if (next && next.ok !== false && hasCurrentContract(next)) {
      envelope = next;
      statusMessage = translate('modelCatalog.updated');
      render();
    } else {
      statusMessage = next?.error || translate('modelCatalog.error');
      render();
    }
  } catch (error: unknown) {
    if (isModelCatalogConflict(error)) {
      const refreshed = await refresh();
      if (refreshed) statusMessage = translate('modelCatalog.conflict');
      return;
    }
    statusMessage = errorMessage(error);
    render();
  } finally {
    busy = false;
    render();
  }
}

function onToggleChange(target: HTMLInputElement): void {
  const kind = target.dataset.mcToggle;
  const id = target.dataset.mcToggleId;
  const checked = target.checked;
  if ((kind !== 'logical' && kind !== 'offering') || !id || !envelope || busy) return;
  // Revert the native control to the last server-confirmed state immediately:
  // a pending mutation must never be presented as an already-applied success.
  const truth = kind === 'logical'
    ? envelope.catalog.models[id]?.enabled !== false
    : (envelope.catalog.offerings[id] as Offering | undefined)?.enabled !== false;
  target.checked = truth;
  void applyToggle(kind, id, checked);
}

function confirmRemove(offeringId: string): Promise<boolean> {
  const retained = (featureRegistry as unknown as {
    showConfirm?: (message: string, opts?: { danger?: boolean }) => Promise<boolean>;
  }).showConfirm;
  if (typeof retained === 'function') {
    return retained(translate('modelCatalog.removeConfirm', { offering: offeringId }), { danger: true });
  }
  return Promise.resolve(
    typeof window !== 'undefined' && typeof window.confirm === 'function'
      ? window.confirm(translate('modelCatalog.removeConfirm', { offering: offeringId }))
      : false,
  );
}

async function onRemoveClick(offeringId: string): Promise<void> {
  if (!envelope || busy) return;
  const ok = await confirmRemove(offeringId);
  if (!ok) return;
  await applyMutation((catalog) => {
    removeOffering(catalog, offeringId);
  });
}

function onClick(target: Element): void {
  if (target.matches('[data-mc-expand]')) {
    onExpandClick(target as HTMLElement);
    return;
  }
  if (target.matches('[data-mc-add]')) {
    openAddForm();
    return;
  }
  if (target.matches('[data-mc-attach]')) {
    openAttachForm((target as HTMLElement).dataset.mcAttach ?? '');
    return;
  }
  if (target.matches('[data-mc-edit]')) {
    openEditForm((target as HTMLElement).dataset.mcEdit ?? '');
    return;
  }
  if (target.matches('[data-mc-remove]')) {
    void onRemoveClick((target as HTMLElement).dataset.mcRemove ?? '');
    return;
  }
  if (target.matches('[data-mc-cancel]')) {
    formState = null;
    statusMessage = null;
    render();
    return;
  }
  if (target.matches('[data-mc-submit]')) {
    submitForm();
    return;
  }
}

async function refresh(): Promise<boolean> {
  const requestGeneration = ++generation;
  statusMessage = null;
  render();
  try {
    const next = await fetchModelCatalog();
    if (requestGeneration !== generation) return false;
    if (next && next.ok !== false && hasCurrentContract(next)) {
      envelope = next;
      render();
      return true;
    } else {
      if (next && next.ok !== false) envelope = null;
      statusMessage = next?.error || translate('modelCatalog.error');
      render();
      return false;
    }
  } catch (error: unknown) {
    if (requestGeneration !== generation) return false;
    statusMessage = errorMessage(error);
    render();
    return false;
  }
}

function ensureScope(): void {
  if (scope) return;
  const root = container();
  if (!(root instanceof HTMLElement)) return;
  const next = createLifecycleScope();
  scope = next;
  next.listen(root, 'change', (event) => {
    const target = event.target;
    if (target instanceof HTMLInputElement) onToggleChange(target);
  });
  next.listen(root, 'input', (event) => {
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement) {
      syncFormField(target);
    }
  });
  next.listen(root, 'click', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    onClick(target);
  });
}

export async function renderModelCatalogPanel(): Promise<void> {
  generation += 1;
  ensureScope();
  await refresh();
}

/** Re-render from the cached envelope without a network round-trip. */
export function repaintModelCatalogPanel(): void {
  if (!envelope) return;
  render();
}

export function destroyModelCatalogPanel(): void {
  generation += 1;
  scope?.destroy();
  scope = null;
  envelope = null;
  statusMessage = null;
  formState = null;
  busy = false;
  expandedIds.clear();
  offeringSectionSeq = 0;
  const root = container();
  if (root) root.replaceChildren();
}

// Feature bridge: the retained Settings runtime reaches these two seams after
// the lazy chunk has evaluated. No other service is published by this owner.
const bridge = featureRegistry as unknown as Record<string, unknown>;
bridge._renderModelCatalogPanel = renderModelCatalogPanel;
bridge._repaintModelCatalogPanel = repaintModelCatalogPanel;
bridge._destroyModelCatalogPanel = destroyModelCatalogPanel;
