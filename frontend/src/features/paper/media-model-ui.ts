import { featureRegistry } from '../../feature-registry';
type LooseObject = Record<string, any>;

interface MediaModel {
  model_id: string;
  provider_id?: string;
  provider_name?: string;
  [key: string]: unknown;
}

type MediaPanel = 'podcast' | 'video';
type MediaGlobals = Window & Record<string, any>;

function globals(): MediaGlobals {
  return featureRegistry as unknown as MediaGlobals;
}

function panelPrefix(panel: MediaPanel): string {
  return panel === 'podcast' ? 'podcast' : 'video';
}

function panelState(panel: MediaPanel): LooseObject | null {
  return (panel === 'podcast' ? globals()._podcast : globals()._pvideo) ?? null;
}

function storageKey(panel: MediaPanel): string {
  return panel === 'podcast' ? 'paperPodcastModel' : 'paperVideoModel';
}

function translate(key: string, fallback: string): string {
  const fn = globals().t;
  return typeof fn === 'function' ? String(fn(key)) : fallback;
}

function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function shortModelName(modelId: string): string {
  const formatter = globals()._modelShortName;
  return typeof formatter === 'function'
    ? String(formatter(modelId))
    : modelId;
}

function chatModels(): MediaModel[] {
  const models = Array.isArray(globals()._registeredModels)
    ? globals()._registeredModels as MediaModel[]
    : [];
  const hidden = globals()._hiddenModels instanceof Set
    ? globals()._hiddenModels as Set<string>
    : new Set<string>();
  const isChatModel = globals().isChatModel;
  return models.filter((model) => Boolean(model?.model_id)
    && !hidden.has(model.model_id)
    && (typeof isChatModel !== 'function' || Boolean(isChatModel(model))));
}

function isPlausibleEpochMs(value: unknown, field: string): boolean {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return false;
  if (numeric < 1e12) {
    console.warn(`[PaperMedia] ${field}=${String(value)} looks like epoch seconds; ignoring`);
    return false;
  }
  if (numeric > Date.now()) {
    console.warn(`[PaperMedia] ${field}=${String(value)} is in the future; ignoring`);
    return false;
  }
  return true;
}

/** Preserve server authority for elapsed and liveness clocks across reattach. */
export function adoptServerClocks(state: LooseObject, source: LooseObject): void {
  if (!state || !source) return;
  if (isPlausibleEpochMs(source.createdAt, 'createdAt')) {
    const started = Number(source.createdAt);
    if (!state.genStartedAt || started < state.genStartedAt) state.genStartedAt = started;
  }
  if (isPlausibleEpochMs(source.updatedAt, 'updatedAt')) {
    const seen = Number(source.updatedAt);
    if (!state.lastEventAt || seen < state.lastEventAt) state.lastEventAt = seen;
  }
}

/** Choose one concrete generation model from saved, toolbar, then catalog state. */
export function seedMediaModel(panel: MediaPanel): void {
  const state = panelState(panel);
  if (!state || state.model) return;
  let saved = '';
  try { saved = localStorage.getItem(storageKey(panel)) || ''; }
  catch (error: unknown) { console.warn('[PaperMedia] read model preference failed:', error); }
  const models = chatModels();
  const ids = new Set(models.map((model) => model.model_id));
  const configured = String(globals().config?.model || globals().serverModel || '');
  state.model = saved && ids.has(saved)
    ? saved
    : configured && ids.has(configured)
      ? configured
      : models[0]?.model_id || '';
}

/** Adopt the making-model of the displayed artifact without relabeling legacy data. */
export function adoptMediaModel(panel: MediaPanel, modelId: string): void {
  const state = panelState(panel);
  if (!state) return;
  state.artifactModel = modelId || '';
  if (!modelId) return;
  state.model = modelId;
  try { localStorage.setItem(storageKey(panel), modelId); }
  catch (error: unknown) { console.warn('[PaperMedia] persist model preference failed:', error); }
}

export function pickMediaOption(button: HTMLElement): void {
  const selectId = button.dataset.sel || '';
  const select = document.getElementById(selectId) as HTMLSelectElement | null;
  if (!select) return;
  select.value = button.dataset.value || '';
  button.parentElement?.querySelectorAll<HTMLElement>('[data-sel]')
    .forEach((sibling) => {
      if (sibling.dataset.sel === selectId) sibling.classList.remove('is-selected');
    });
  button.classList.add('is-selected');
  const persistPodcastOption = globals()._pcPickPersist;
  if (typeof persistPodcastOption === 'function') {
    persistPodcastOption(selectId, select.value);
  }
}

function compareModels(left: unknown, right: unknown): number {
  const compare = globals()._compareModelsByDisplayName;
  if (typeof compare === 'function') return Number(compare(left, right)) || 0;
  const leftName = typeof left === 'string'
    ? left
    : String((left as MediaModel | null)?.model_id || '');
  const rightName = typeof right === 'string'
    ? right
    : String((right as MediaModel | null)?.model_id || '');
  return leftName.localeCompare(rightName);
}

export function populateModelDropdown(panel: MediaPanel): void {
  const prefix = panelPrefix(panel);
  const dropdown = document.getElementById(`${prefix}ModelDropdown`);
  if (!dropdown) return;
  seedMediaModel(panel);
  const state = panelState(panel);
  const grouped = new Map<string, { name: string; models: MediaModel[] }>();
  for (const model of chatModels()) {
    const providerId = model.provider_id || 'default';
    const group = grouped.get(providerId) ?? {
      name: model.provider_name || providerId,
      models: [],
    };
    group.models.push(model);
    grouped.set(providerId, group);
  }
  dropdown.replaceChildren();
  const groups = [...grouped.values()].sort((left, right) =>
    compareModels(left.name, right.name));
  for (const group of groups) {
    group.models.sort(compareModels);
    if (groups.length > 1) {
      const section = document.createElement('div');
      section.className = 'paper-report-model-dropdown-section';
      section.textContent = group.name;
      dropdown.appendChild(section);
    }
    for (const model of group.models) {
      const item = document.createElement('div');
      item.className = 'paper-report-model-dropdown-item'
        + (model.model_id === state?.model ? ' active' : '');
      item.textContent = shortModelName(model.model_id);
      item.title = model.model_id;
      item.addEventListener('click', () => selectMediaModel(panel, model.model_id));
      dropdown.appendChild(item);
    }
  }
}

export function selectMediaModel(panel: MediaPanel, modelId: string): void {
  const state = panelState(panel);
  if (!state) return;
  state.model = modelId || '';
  try { localStorage.setItem(storageKey(panel), state.model); }
  catch (error: unknown) { console.warn('[PaperMedia] persist model preference failed:', error); }
  const prefix = panelPrefix(panel);
  const label = document.getElementById(`${prefix}ModelLabel`);
  if (label) label.textContent = shortModelName(modelId);
  const dropdown = document.getElementById(`${prefix}ModelDropdown`);
  dropdown?.classList.remove('open');
  dropdown?.querySelectorAll<HTMLElement>('.paper-report-model-dropdown-item')
    .forEach((item) => item.classList.toggle('active', item.title === modelId));
}

export function toggleModelDropdown(event: Event | null, panel: MediaPanel): void {
  event?.stopPropagation();
  const dropdown = document.getElementById(`${panelPrefix(panel)}ModelDropdown`);
  if (!dropdown) return;
  if (!dropdown.classList.contains('open')) populateModelDropdown(panel);
  dropdown.classList.toggle('open');
}

function modelIcon(kind: 'chip' | 'chevron'): string {
  if (kind === 'chevron') {
    return '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
  }
  return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/></svg>';
}

function modelButtonContents(state: LooseObject | null): string {
  const current = String(state?.model || '');
  const label = current
    ? shortModelName(current)
    : translate('paper.reportSelectModel', 'Select model');
  return `${modelIcon('chip')}<span class="pm-model-label">${escapeHtml(label)}</span>${modelIcon('chevron')}`;
}

export function modelFieldHtml(panel: MediaPanel, state: LooseObject): string {
  const prefix = panelPrefix(panel);
  return '<div class="pm-field"><div class="pm-field-label">'
    + escapeHtml(translate('paper.mediaOptModel', 'Model')) + '</div>'
    + '<div class="pm-model"><button type="button" class="pm-model-btn"'
    + ` id="${prefix}ModelBtn" title="`
    + escapeHtml(translate('paper.mediaModelTitle', 'Model used for generation')) + '"'
    + ` data-tofu-action="_pmToggleModelDropdown(event,'${panel}')">`
    + modelButtonContents(state).replace('class="pm-model-label"',
      `class="pm-model-label" id="${prefix}ModelLabel"`)
    + `</button><div class="paper-report-model-dropdown" id="${prefix}ModelDropdown"></div>`
    + '</div></div>';
}

export function modelInlineHtml(panel: MediaPanel, state: LooseObject): string {
  const prefix = panelPrefix(panel);
  return '<div class="pm-model-inline">'
    + '<button type="button" class="paper-podcast-btn paper-podcast-btn-ghost pm-model-inline-btn"'
    + ' title="' + escapeHtml(translate(
      'paper.mediaModelTitle', 'Model used for generation')) + '"'
    + ` data-tofu-action="_pmToggleModelDropdown(event,'${panel}')">`
    + modelButtonContents(state).replace('class="pm-model-label"',
      `class="pm-model-label" id="${prefix}ModelLabel"`)
    + `</button><div class="paper-report-model-dropdown" id="${prefix}ModelDropdown"></div>`
    + '</div>';
}

function installMediaModelGlobals(): void {
  const target = globals();
  target._pmAdoptServerClocks = adoptServerClocks;
  target._pmSeedModel = seedMediaModel;
  target._pmAdoptModel = adoptMediaModel;
  target._pmPick = pickMediaOption;
  target._pmShortName = shortModelName;
  target._pmPopulateModelDropdown = populateModelDropdown;
  target._pmSelectModel = selectMediaModel;
  target._pmToggleModelDropdown = toggleModelDropdown;
  target._pmModelFieldHtml = modelFieldHtml;
  target._pmModelInlineHtml = modelInlineHtml;
}

document.addEventListener('click', () => {
  for (const id of ['podcastModelDropdown', 'videoModelDropdown']) {
    document.getElementById(id)?.classList.remove('open');
  }
});

installMediaModelGlobals();
