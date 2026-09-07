/** Skills panel state, rendering, commands, rollback, and lifecycle owner. */
import { featureRegistry } from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
import type { I18nKey } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import {
  attachSkillsPackageDropZone,
  promptInstallScope,
  showSkillsToast,
} from './package-install-panel';

type SkillScope = 'catalog' | 'installed';
type InstallScope = 'global' | 'project';

interface SkillRequirements {
  bins?: string[];
  env?: string[];
}

interface CatalogSkill {
  id: string;
  name: string;
  description?: string;
  tags?: string[];
  author?: string;
  category?: string;
  icon?: string;
  featured?: boolean;
  homepage?: string;
  install_note?: string;
  requires?: SkillRequirements;
  installed?: boolean;
  installed_memory_id?: string;
  installable?: boolean;
  unavailable_reason?: string;
  catalog_id?: string;
  source?: string;
  source_revision?: string;
  verified?: boolean;
  signed?: boolean;
  publisher?: string;
  downloads?: number;
  installed_source_revision?: string;
  update_available?: boolean;
  remote_rank?: number;
}

interface InstalledSkill {
  id: string;
  name: string;
  description?: string;
  tags?: string[];
  scope?: string;
  enabled?: boolean;
  eligible?: boolean;
  ineligible_reasons?: string[];
  is_package?: boolean;
  requires_env?: string[];
  updated?: string;
  catalog_id?: string;
  source_revision?: string;
  source_registry?: string;
}

interface SkillEnvRow {
  name: string;
  declared?: boolean;
  configured?: boolean;
  hint?: string;
}

interface SkillFile {
  path: string;
  size: number;
  kind?: string;
}

interface ResponseLike {
  ok: boolean;
  status?: number;
  statusText?: string;
  json(): Promise<Record<string, unknown>>;
}

interface SkillsApi {
  catalog(): Promise<{ catalog?: CatalogSkill[] } | null>;
  catalogSearch(query: string, limit: number): Promise<{
    catalog?: CatalogSkill[];
    online?: {
      ok?: boolean;
      error?: string;
      cached?: boolean;
      verified_count?: number;
    };
  } | null>;
  list(scope: string): Promise<{ skills?: InstalledSkill[] } | null>;
  envStatus(id: string): Promise<{ env?: SkillEnvRow[] } | null>;
  envSet(id: string, name: string, value: string): Promise<ResponseLike | null>;
  envDelete(id: string, name: string): Promise<ResponseLike | null>;
  setScope(id: string, scope: InstallScope): Promise<ResponseLike | null>;
  catalogInstall(
    id: string,
    scope: InstallScope,
    sourceRevision?: string,
    overwrite?: boolean,
  ): Promise<ResponseLike | null>;
  uninstall(id: string): Promise<ResponseLike | null>;
  toggle(id: string): Promise<ResponseLike | null>;
  files(id: string): Promise<{
    count?: number;
    root?: string;
    files?: SkillFile[];
  } | null>;
}

type SkillsWindow = Window & {
  Api?: { skills?: SkillsApi };
  t?: (key: string, values?: Record<string, unknown>) => string;
  Icon?: (name: string, size?: number) => string;
  debugLog?: (message: string, kind?: string) => void;
  showConfirm?: (message: string, options?: { danger?: boolean }) => Promise<boolean>;
  _populateSkillsTab?: () => Promise<void>;
  _skillsSetScope?: (scope: SkillScope) => void;
  _skillsFilter?: (query: string) => void;
  _skillsSetCategory?: (category: string) => void;
  _skillsSetPage?: (page: number) => void;
  _skillsRender?: () => void;
  _skillsEnvToggleEditor?: (skillId: string, envName: string) => void;
  _skillsEnvSave?: (skillId: string, envName: string) => Promise<void>;
  _skillsEnvDelete?: (skillId: string, envName: string) => Promise<void>;
  _skillsMoveScope?: (
    skillId: string,
    scope: InstallScope,
    button?: HTMLButtonElement | null,
  ) => Promise<void>;
  _skillsCatalogInstall?: (
    skillId: string,
    button?: HTMLButtonElement | null,
    sourceRevision?: string,
  ) => Promise<void>;
  _skillsUninstall?: (skillId: string) => Promise<void>;
  _skillsToggleEnabled?: (
    skillId: string,
    button?: HTMLButtonElement | null,
  ) => Promise<void>;
  _skillsViewFiles?: (skillId: string) => Promise<void>;
  _skillsCloseFiles?: (event?: Event) => void;
  _destroySkills?: () => void;
};

const PAGE_SIZE = 12;
const ONLINE_SEARCH_LIMIT = 8;
const ONLINE_SEARCH_DEBOUNCE_MS = 350;
const CATEGORY_ORDER = [
  'Documents', 'Coding', 'Creative', 'Infrastructure',
  'Productivity', 'Research', 'Other',
] as const;

let catalog: CatalogSkill[] = [];
let onlineCatalog: CatalogSkill[] = [];
let installed: InstalledSkill[] = [];
let scope: SkillScope = 'catalog';
let activeCategory = 'all';
let searchQuery = '';
let page = 1;
let envStatus: Record<string, SkillEnvRow[]> = {};
let viewScope: LifecycleScope | null = null;
let populateGeneration = 0;
let filesGeneration = 0;
let dataEpoch = 0;
let onlineSearchTimer: number | null = null;
let onlineSearchGeneration = 0;
let onlineSearchPending = false;
let onlineSearchError = '';
let onlineSearchQuery = '';

function globals(): SkillsWindow {
  return featureRegistry as unknown as SkillsWindow;
}

function api(): SkillsApi {
  const value = globals().Api?.skills;
  if (!value) throw new Error('Skills API is not ready');
  return value;
}

function translate(key: I18nKey, values?: Record<string, unknown>): string {
  return globals().t?.(key, values) || key;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function toast(message: string, kind?: 'error' | 'success'): void {
  showSkillsToast(message, kind);
}

function debug(message: string, kind?: string): void {
  globals().debugLog?.(message, kind);
}

function icon(name: string, size: number): string {
  return globals().Icon?.(name, size) || '';
}

async function json(response: ResponseLike | null): Promise<Record<string, unknown>> {
  if (!response) return {};
  try {
    return await response.json();
  } catch {
    return {};
  }
}

async function requireOk(response: ResponseLike | null): Promise<Record<string, unknown>> {
  const body = await json(response);
  if (!response?.ok) {
    const detail = typeof body.error === 'string'
      ? body.error
      : response?.statusText || translate('skills.noResponse');
    throw new Error(detail);
  }
  return body;
}

function ensureLifecycle(): LifecycleScope {
  if (viewScope && !viewScope.signal.aborted) return viewScope;
  const lifecycle = createLifecycleScope();
  viewScope = lifecycle;
  const panel = document.getElementById('settingsTab_skills');
  if (panel) lifecycle.listen(panel, 'click', onPanelClick);
  return lifecycle;
}

export function destroySkills(): void {
  populateGeneration += 1;
  filesGeneration += 1;
  onlineSearchGeneration += 1;
  if (onlineSearchTimer !== null) window.clearTimeout(onlineSearchTimer);
  onlineSearchTimer = null;
  onlineSearchPending = false;
  viewScope?.destroy();
  viewScope = null;
  closeFiles();
}

export async function populateSkillsTab(): Promise<void> {
  const lifecycle = ensureLifecycle();
  const generation = ++populateGeneration;
  try {
    const [catalogData, listData] = await Promise.all([
      api().catalog(),
      api().list('all'),
    ]);
    if (lifecycle.signal.aborted || generation !== populateGeneration) return;

    const nextCatalog = Array.isArray(catalogData?.catalog) ? catalogData.catalog : [];
    const all = Array.isArray(listData?.skills) ? listData.skills : [];
    const nextInstalled = all.filter((item) => item.is_package);
    const nextEnv: Record<string, SkillEnvRow[]> = {};
    let envFailures = 0;
    await Promise.all(nextInstalled.map(async (item) => {
      if (!item.requires_env?.length) return;
      try {
        const result = await api().envStatus(item.id);
        if (Array.isArray(result?.env)) nextEnv[item.id] = result.env;
      } catch {
        envFailures += 1;
      }
    }));
    if (lifecycle.signal.aborted || generation !== populateGeneration) return;

    catalog = nextCatalog;
    installed = nextInstalled;
    refreshOnlineInstalledState();
    envStatus = nextEnv;
    dataEpoch += 1;
    render();
    scheduleOnlineSearch(searchQuery);
    attachSkillsPackageDropZone();
    if (envFailures) {
      debug(`[Skills] ${envFailures} credential status request(s) failed`, 'warn');
    }
  } catch (error: unknown) {
    if (lifecycle.signal.aborted || generation !== populateGeneration) return;
    const message = errorMessage(error);
    debug(`[Skills] Failed to load: ${message}`, 'error');
    const grid = document.getElementById('skillsCatalogGrid');
    if (grid) {
      grid.innerHTML = `<p class="stg-empty">${escape(
        translate('skills.loadFailed', { err: message }),
      )}</p>`;
    }
  }
}

export function setScope(nextScope: SkillScope): void {
  if (nextScope !== 'catalog' && nextScope !== 'installed') return;
  scope = nextScope;
  page = 1;
  document.querySelectorAll<HTMLElement>('.skills-scope-tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.scope === nextScope);
  });
  render();
  scheduleOnlineSearch(searchQuery);
}

export function filterSkills(query: string): void {
  searchQuery = String(query || '').toLowerCase().trim();
  page = 1;
  render();
  scheduleOnlineSearch(searchQuery);
}

function refreshOnlineInstalledState(): void {
  const byCatalog = new Map(
    installed
      .filter((item) => item.catalog_id)
      .map((item) => [item.catalog_id as string, item]),
  );
  for (const entry of onlineCatalog) {
    const item = byCatalog.get(entry.catalog_id || entry.id);
    entry.installed = Boolean(item);
    entry.installed_memory_id = item?.id || '';
    entry.installed_source_revision = item?.source_revision || '';
    entry.update_available = Boolean(
      item?.source_revision
      && entry.source_revision
      && item.source_revision !== entry.source_revision,
    );
  }
}

function scheduleOnlineSearch(query: string): void {
  onlineSearchGeneration += 1;
  const generation = onlineSearchGeneration;
  if (onlineSearchTimer !== null) window.clearTimeout(onlineSearchTimer);
  onlineSearchTimer = null;
  const normalized = String(query || '').trim();
  if (scope !== 'catalog' || normalized.length < 2) {
    onlineCatalog = [];
    onlineSearchQuery = '';
    onlineSearchPending = false;
    onlineSearchError = '';
    render();
    return;
  }
  onlineSearchPending = true;
  onlineSearchError = '';
  render();
  onlineSearchTimer = window.setTimeout(() => {
    onlineSearchTimer = null;
    void runOnlineSearch(normalized, generation);
  }, ONLINE_SEARCH_DEBOUNCE_MS);
}

async function runOnlineSearch(query: string, generation: number): Promise<void> {
  try {
    const result = await api().catalogSearch(query, ONLINE_SEARCH_LIMIT);
    if (generation !== onlineSearchGeneration || viewScope?.signal.aborted) return;
    const remote = Array.isArray(result?.catalog) ? result.catalog : [];
    onlineCatalog = remote.map((entry, index) => ({
      ...entry,
      remote_rank: index,
    }));
    onlineSearchQuery = query;
    onlineSearchError = result?.online?.ok === false
      ? result.online.error || 'online_unavailable'
      : '';
    refreshOnlineInstalledState();
  } catch (error: unknown) {
    if (generation !== onlineSearchGeneration || viewScope?.signal.aborted) return;
    onlineCatalog = [];
    onlineSearchQuery = query;
    onlineSearchError = errorMessage(error);
  } finally {
    if (generation === onlineSearchGeneration && !viewScope?.signal.aborted) {
      onlineSearchPending = false;
      render();
    }
  }
}

export function setCategory(category: string): void {
  activeCategory = category || 'all';
  page = 1;
  render();
}

export function setPage(nextPage: number): void {
  page = Math.max(1, Number(nextPage) | 0);
  render();
  const grid = document.getElementById('skillsCatalogGrid');
  try {
    grid?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch {
    // Smooth scrolling is optional in embedded webviews.
  }
}

function renderPagination(total: number): string {
  const pages = Math.ceil(total / PAGE_SIZE);
  if (pages <= 1) return '';
  page = Math.min(page, pages);
  const numbers = new Set<number>([1, pages]);
  for (let delta = -2; delta <= 2; delta += 1) numbers.add(page + delta);
  const visible = [...numbers].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  const from = (page - 1) * PAGE_SIZE + 1;
  const to = Math.min(total, page * PAGE_SIZE);
  let controls = '';
  let previous = 0;
  for (const number of visible) {
    if (previous && number - previous > 1) {
      controls += '<span class="skills-page-ellipsis">…</span>';
    }
    controls += `<button class="skills-page-btn${number === page ? ' is-active' : ''}"
      data-skills-action="page" data-page="${number}">${number}</button>`;
    previous = number;
  }
  return `<div class="skills-pagination">
    <span class="skills-page-info">${escape(translate('skills.pageInfo', { from, to, total }))}</span>
    <div class="skills-page-ctrls">
      <button class="skills-page-btn" data-skills-action="page" data-page="${page - 1}"
        ${page <= 1 ? 'disabled' : ''} aria-label="Previous page">‹</button>
      ${controls}
      <button class="skills-page-btn" data-skills-action="page" data-page="${page + 1}"
        ${page >= pages ? 'disabled' : ''} aria-label="Next page">›</button>
    </div>
  </div>`;
}

function orderedCategories(counts: Record<string, number>): string[] {
  const known = CATEGORY_ORDER.filter((category) => counts[category]);
  const extras = Object.keys(counts)
    .filter((category) => !(CATEGORY_ORDER as readonly string[]).includes(category))
    .sort();
  return [...known, ...extras];
}

function renderHeader(): void {
  const total = document.getElementById('skillsTotalCount');
  const count = document.getElementById('skillsCatalogCount');
  if (total) total.textContent = translate('skills.countInstalled', { n: installed.length });
  if (count) {
    const catalogCount = searchQuery ? filteredCatalog().length : catalog.length;
    count.textContent = translate('skills.countCatalog', { n: catalogCount });
    count.style.display = scope === 'catalog' ? '' : 'none';
  }
}

function renderCategoryBar(): void {
  const bar = document.getElementById('skillsCategoryBar');
  if (!bar) return;
  if (scope !== 'catalog') {
    bar.replaceChildren();
    bar.style.display = 'none';
    return;
  }
  bar.style.display = '';
  const counts: Record<string, number> = {};
  const categoryEntries = searchQuery && onlineSearchQuery === searchQuery
    ? [...catalog, ...onlineCatalog]
    : catalog;
  for (const entry of categoryEntries) {
    const category = entry.category || 'Other';
    counts[category] = (counts[category] || 0) + 1;
  }
  let html = `<button class="mcp-cat-pill${activeCategory === 'all' ? ' active' : ''}"
    data-skills-action="category" data-category="all">${escape(translate('skills.scopeAll'))}
    <span class="mcp-cat-count">${categoryEntries.length}</span></button>`;
  for (const category of orderedCategories(counts)) {
    html += `<button class="mcp-cat-pill${activeCategory === category ? ' active' : ''}"
      data-skills-action="category" data-category="${escape(category)}">${escape(category)}
      <span class="mcp-cat-count">${counts[category]}</span></button>`;
  }
  bar.innerHTML = html;
}

function filteredCatalog(): CatalogSkill[] {
  const localMatches = catalog.filter((entry) => {
    if (activeCategory !== 'all' && entry.category !== activeCategory) return false;
    if (!searchQuery) return true;
    const haystack = [entry.name, entry.description, ...(entry.tags || []), entry.author]
      .filter(Boolean).join(' ').toLowerCase();
    return haystack.includes(searchQuery);
  });
  if (!searchQuery || onlineSearchQuery !== searchQuery) return localMatches;
  const remoteMatches = onlineCatalog.filter((entry) => (
    activeCategory === 'all' || entry.category === activeCategory
  ));
  const merged = new Map<string, CatalogSkill>();
  for (const entry of [...localMatches, ...remoteMatches]) {
    const key = entry.catalog_id || entry.id;
    if (!merged.has(key)) merged.set(key, entry);
  }
  return [...merged.values()];
}

function safeHomepage(value?: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.href);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function catalogCard(entry: CatalogSkill): string {
  const isInstalled = Boolean(entry.installed);
  const isUnavailable = entry.installable === false;
  const rawIcon = entry.icon || icon('package', 26);
  const iconHtml = /^<svg[\s>]/i.test(rawIcon) ? rawIcon : escape(rawIcon);
  let badges = '';
  if (entry.featured) badges += `<span class="skill-badge-featured">${escape(translate('skills.featured'))}</span>`;
  if (entry.author && /anthropic/i.test(entry.author)) {
    badges += `<span class="skill-badge-official">${escape(translate('skills.official'))}</span>`;
  }
  if (entry.source === 'clawhub') {
    badges += `<span class="skill-badge-official">ClawHub</span>`;
  }
  if (entry.verified) {
    badges += `<span class="skill-badge-official">${escape(translate('skills.verified'))}</span>`;
  }
  const requirements = entry.requires || {};
  const warnings: string[] = [];
  if (requirements.bins?.length) {
    warnings.push(translate('skills.reqBins', { bins: requirements.bins.join(', ') }));
  }
  if (requirements.env?.length) {
    warnings.push(translate('skills.reqEnv', { env: requirements.env.join(', ') }));
  }
  if (isUnavailable && entry.unavailable_reason) {
    warnings.push(entry.unavailable_reason);
  }
  const homepage = safeHomepage(entry.homepage);
  const memoryId = entry.installed_memory_id || entry.id;
  const updateAction = entry.update_available && !isUnavailable
    ? `<button class="btn btn-primary btn-xs" data-skills-action="install"
         data-skill-id="${escape(entry.catalog_id || entry.id)}"
         data-source-revision="${escape(entry.source_revision || '')}"
         data-overwrite="true">${escape(translate('skills.updateBtn'))}</button>`
    : '';
  const actions = isInstalled
    ? `<span class="skill-installed-tag">${escape(translate('skills.installedTag'))}</span>
       ${updateAction}
       <button class="btn btn-secondary btn-xs" data-skills-action="files"
         data-skill-id="${escape(memoryId)}">${escape(translate('skills.viewFiles'))}</button>
       <button class="btn btn-secondary btn-xs" data-skills-action="uninstall"
         data-skill-id="${escape(memoryId)}">${escape(translate('skills.uninstallBtn'))}</button>`
    : isUnavailable
      ? `<button class="btn btn-primary btn-xs" disabled aria-disabled="true"
           title="${escape(entry.unavailable_reason || '')}">${escape(translate('skills.installBtn'))}</button>`
      : `<button class="btn btn-primary btn-xs" data-skills-action="install"
           data-skill-id="${escape(entry.catalog_id || entry.id)}"
           data-source-revision="${escape(entry.source_revision || '')}">${escape(translate('skills.installBtn'))}</button>`;
  return `<div class="mcp-app-card skill-card${isInstalled ? ' is-installed' : ''}">
    <div class="mcp-app-icon">${iconHtml}</div>
    <div class="mcp-app-name"><span class="mcp-app-name-text">${escape(entry.name)}</span>${badges}</div>
    ${entry.author ? `<div class="skill-author">${escape(translate('skills.by', { author: entry.author }))}</div>` : ''}
    <div class="mcp-app-desc">${escape(entry.description || '')}</div>
    ${entry.install_note ? `<div class="mcp-app-note">${escape(entry.install_note)}</div>` : ''}
    ${entry.source_revision ? `<div class="mcp-app-note">${escape(translate('skills.version', { version: entry.source_revision }))}</div>` : ''}
    ${warnings.length ? `<div class="skill-badge-warn">⚠ ${escape(warnings.join(' · '))}</div>` : ''}
    <div class="skill-card-footer">
      ${homepage ? `<a class="mcp-app-repo" href="${escape(homepage)}" target="_blank" rel="noopener" title="Homepage">${repoIcon()} ${escape(translate('skills.repo'))}</a>` : '<span></span>'}
      <div class="skill-card-actions">${actions}</div>
    </div>
  </div>`;
}

function repoIcon(): string {
  return '<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>';
}

function envDomId(prefix: string, skillId: string, envName: string): string {
  return `${prefix}_${encodeURIComponent(skillId)}_${encodeURIComponent(envName)}`;
}

function envSection(item: InstalledSkill): string {
  const rows = envStatus[item.id];
  if (!rows?.length) return '';
  return `<div class="skill-env-section">${rows.map((row) => {
    const state = row.configured
      ? `<span class="skill-env-state is-ok">${escape(translate('skills.envConfigured'))}${row.hint ? ` · ${escape(row.hint)}` : ''}</span>`
      : `<span class="skill-env-state is-missing">${escape(translate('skills.envMissing'))}</span>`;
    return `<div class="skill-env-row">
        <code class="skill-env-name">${escape(row.name)}</code>${state}
        <button class="btn btn-secondary btn-xs" data-skills-action="env-edit"
          data-skill-id="${escape(item.id)}" data-env-name="${escape(row.name)}">${escape(translate(row.configured ? 'skills.envUpdate' : 'skills.envSet'))}</button>
        ${row.configured ? `<button class="btn btn-secondary btn-xs" data-skills-action="env-delete"
          data-skill-id="${escape(item.id)}" data-env-name="${escape(row.name)}">${escape(translate('skills.envDelete'))}</button>` : ''}
      </div>
      <div class="skill-env-editor" id="${escape(envDomId('skillEnvEditor', item.id, row.name))}" style="display:none">
        <input type="password" class="skill-env-input" id="${escape(envDomId('skillEnvInput', item.id, row.name))}"
          placeholder="${escape(translate('skills.envPlaceholder'))}" autocomplete="off">
        <button class="btn btn-primary btn-xs" data-skills-action="env-save"
          data-skill-id="${escape(item.id)}" data-env-name="${escape(row.name)}">${escape(translate('skills.envSave'))}</button>
      </div>`;
  }).join('')}</div>`;
}

function installedCard(item: InstalledSkill): string {
  const targetScope: InstallScope = item.scope === 'global' ? 'project' : 'global';
  return `<div class="mcp-app-card skill-card is-installed">
    <div class="mcp-app-icon">${icon('package', 26)}</div>
    <div class="mcp-app-name"><span class="mcp-app-name-text">${escape(item.name)}</span>
      <span class="mcp-app-status ${item.enabled ? 'on' : 'off'}"><span class="dot"></span>${escape(translate(item.enabled ? 'skills.statusOn' : 'skills.statusOff'))}</span>
    </div>
    <div class="skill-author">${escape(translate('skills.scopeIdLine', { scope: item.scope, id: item.id }))}</div>
    <div class="mcp-app-desc">${escape(item.description || '')}</div>
    ${item.eligible === false && item.ineligible_reasons?.length
      ? `<div class="skill-badge-warn">⚠ ${escape(item.ineligible_reasons.join(' · '))}</div>` : ''}
    ${envSection(item)}
    <div class="skill-card-footer"><span></span><div class="skill-card-actions">
      <button class="btn btn-secondary btn-xs" data-skills-action="files" data-skill-id="${escape(item.id)}">${escape(translate('skills.viewFiles'))}</button>
      <button class="btn btn-secondary btn-xs" data-skills-action="move" data-skill-id="${escape(item.id)}"
        data-target-scope="${targetScope}">${escape(translate(targetScope === 'project' ? 'skills.moveToProject' : 'skills.moveToGlobal'))}</button>
      <button class="btn btn-secondary btn-xs" data-skills-action="toggle" data-skill-id="${escape(item.id)}">${escape(translate(item.enabled ? 'skills.disable' : 'skills.enable'))}</button>
      <button class="btn btn-secondary btn-xs" data-skills-action="uninstall" data-skill-id="${escape(item.id)}">${escape(translate('skills.uninstallBtn'))}</button>
    </div></div>
  </div>`;
}

function renderCatalog(): void {
  const grid = document.getElementById('skillsCatalogGrid');
  if (!grid) return;
  const items = filteredCatalog().sort((a, b) => {
    if (searchQuery) {
      const aRemote = a.source === 'clawhub';
      const bRemote = b.source === 'clawhub';
      if (aRemote !== bRemote) return aRemote ? 1 : -1;
      if (aRemote && bRemote) {
        return (a.remote_rank ?? Number.MAX_SAFE_INTEGER)
          - (b.remote_rank ?? Number.MAX_SAFE_INTEGER);
      }
    }
    if (a.featured !== b.featured) return a.featured ? -1 : 1;
    return (a.name || '').localeCompare(b.name || '');
  });
  if (!items.length) {
    const message = onlineSearchPending
      ? translate('skills.onlineSearching')
      : onlineSearchError
        ? translate('skills.onlineSearchFailed')
        : translate('skills.noMatch');
    grid.innerHTML = `<p class="stg-empty">${escape(message)}</p>`;
    return;
  }
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  page = Math.min(page, pages);
  const start = (page - 1) * PAGE_SIZE;
  const onlineStatus = onlineSearchPending
    ? `<p class="stg-empty">${escape(translate('skills.onlineSearching'))}</p>`
    : onlineSearchError
      ? `<p class="stg-empty">${escape(translate('skills.onlineSearchFailed'))}</p>`
      : '';
  grid.innerHTML = items.slice(start, start + PAGE_SIZE).map(catalogCard).join('')
    + onlineStatus + renderPagination(items.length);
}

function renderInstalled(): void {
  const grid = document.getElementById('skillsCatalogGrid');
  if (!grid) return;
  let items = installed.filter((item) => {
    if (!searchQuery) return true;
    return [item.name, item.description, ...(item.tags || [])]
      .filter(Boolean).join(' ').toLowerCase().includes(searchQuery);
  });
  if (!items.length) {
    grid.innerHTML = `<p class="stg-empty">${escape(translate('skills.emptyInstalled'))}</p>`;
    return;
  }
  items.sort((a, b) => (b.updated || '').localeCompare(a.updated || ''));
  const total = items.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  page = Math.min(page, pages);
  items = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  grid.innerHTML = items.map(installedCard).join('') + renderPagination(total);
}

export function render(): void {
  renderHeader();
  renderCategoryBar();
  if (scope === 'catalog') renderCatalog();
  else renderInstalled();
}

export function toggleEnvEditor(skillId: string, envName: string): void {
  const editor = document.getElementById(envDomId('skillEnvEditor', skillId, envName));
  if (!(editor instanceof HTMLElement)) return;
  editor.style.display = editor.style.display === 'none' ? 'flex' : 'none';
  if (editor.style.display !== 'none') {
    document.getElementById(envDomId('skillEnvInput', skillId, envName))?.focus();
  }
}

export async function saveEnv(skillId: string, envName: string): Promise<void> {
  const input = document.getElementById(envDomId('skillEnvInput', skillId, envName));
  const value = input instanceof HTMLInputElement ? input.value.trim() : '';
  if (!value) {
    toast(translate('skills.envEmpty'), 'error');
    return;
  }
  try {
    await requireOk(await api().envSet(skillId, envName, value));
    toast(translate('skills.envSaved', { name: envName }), 'success');
    await populateSkillsTab();
  } catch (error: unknown) {
    toast(translate('skills.envSaveFailed', { err: errorMessage(error) }), 'error');
  }
}

export async function deleteEnv(skillId: string, envName: string): Promise<void> {
  const confirmed = await (globals().showConfirm?.(
    translate('skills.envDeleteConfirm', { name: envName }), { danger: true },
  ) ?? Promise.resolve(true));
  if (!confirmed) return;
  try {
    await requireOk(await api().envDelete(skillId, envName));
    toast(translate('skills.envDeleted', { name: envName }), 'success');
    await populateSkillsTab();
  } catch (error: unknown) {
    toast(translate('skills.envDeleteFailed', { err: errorMessage(error) }), 'error');
  }
}

export async function moveScope(
  skillId: string,
  targetScope: InstallScope,
  button?: HTMLButtonElement | null,
): Promise<void> {
  if (button) button.disabled = true;
  try {
    await requireOk(await api().setScope(skillId, targetScope));
    toast(translate('skills.scopeMoved', { id: skillId }), 'success');
    await populateSkillsTab();
  } catch (error: unknown) {
    toast(translate('skills.scopeMoveFailed', { err: errorMessage(error) }), 'error');
    if (button?.isConnected) button.disabled = false;
  }
}

export async function catalogInstall(
  skillId: string,
  button?: HTMLButtonElement | null,
  sourceRevision = '',
  overwrite = false,
): Promise<void> {
  const entry = [...catalog, ...onlineCatalog].find((candidate) => (
    (candidate.catalog_id || candidate.id) === skillId
  ));
  const installScope = await promptInstallScope(entry?.name || skillId);
  if (!installScope) return;
  if (entry?.source === 'clawhub') {
    const confirmed = await (globals().showConfirm?.(
      translate(overwrite ? 'skills.remoteUpdateConfirm' : 'skills.remoteInstallConfirm', {
        name: entry.name || skillId,
        version: sourceRevision || entry.source_revision || '',
      }),
    ) ?? Promise.resolve(true));
    if (!confirmed) return;
  }
  if (button) {
    button.disabled = true;
    button.textContent = translate(overwrite ? 'skills.updating' : 'skills.installing');
  }
  toast(translate('skills.downloadingInstalling', { id: skillId }));
  try {
    const response = await api().catalogInstall(
      skillId,
      installScope,
      sourceRevision || entry?.source_revision || '',
      overwrite,
    );
    const body = await json(response);
    if (!response?.ok) {
      const detail = typeof body.error === 'string'
        ? body.error
        : response?.statusText || translate('skills.noResponse');
      toast(translate('skills.installFailed', { err: detail }), 'error');
      if (button?.isConnected) {
        button.disabled = false;
        button.textContent = translate(overwrite ? 'skills.updateBtn' : 'skills.installBtn');
      }
      return;
    }
    const memory = body.memory && typeof body.memory === 'object'
      ? body.memory as Record<string, unknown> : {};
    const hints = Array.isArray(body.install_hints)
      ? body.install_hints as Array<Record<string, unknown>> : [];
    let message = translate('skills.installedToast', { name: memory.name || skillId });
    if (hints.length) {
      message += translate('skills.installHintSuffix', {
        files: hints.map((hint) => String(hint.file || '')).filter(Boolean).join(', '),
      });
    }
    toast(message, 'success');
    debug(`[Skills] Installed: ${String(memory.name || skillId)}`, 'success');
    await populateSkillsTab();
  } catch (error: unknown) {
    toast(translate('skills.installError', { err: errorMessage(error) }), 'error');
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = translate(overwrite ? 'skills.updateBtn' : 'skills.installBtn');
    }
  }
}

export async function uninstallSkill(memoryId: string): Promise<void> {
  let confirmText = translate('skills.uninstallConfirm', { id: memoryId });
  if (envStatus[memoryId]?.some((row) => row.configured)) {
    confirmText += `\n${translate('skills.uninstallConfirmEnv')}`;
  }
  const confirmed = await (globals().showConfirm?.(confirmText, { danger: true })
    ?? Promise.resolve(true));
  if (!confirmed) return;

  const epoch = dataEpoch;
  const index = installed.findIndex((item) => item.id === memoryId);
  const removed = index >= 0 ? installed[index] : null;
  const catalogEntry = catalog.find((entry) => (
    entry.installed_memory_id || entry.id
  ) === memoryId) || null;
  if (removed) installed = installed.filter((item) => item !== removed);
  if (catalogEntry) {
    catalogEntry.installed = false;
    delete catalogEntry.installed_memory_id;
  }
  render();
  try {
    await requireOk(await api().uninstall(memoryId));
    toast(translate('skills.uninstalledToast', { id: memoryId }), 'success');
    await populateSkillsTab();
  } catch (error: unknown) {
    if (dataEpoch === epoch) {
      if (removed && !installed.includes(removed)) {
        installed = installed.slice();
        installed.splice(Math.min(index, installed.length), 0, removed);
      }
      if (catalogEntry) {
        catalogEntry.installed = true;
        catalogEntry.installed_memory_id = memoryId;
      }
      render();
    }
    toast(translate('skills.uninstallFailed', { err: errorMessage(error) }), 'error');
  }
}

export async function toggleEnabled(
  memoryId: string,
  _button?: HTMLButtonElement | null,
): Promise<void> {
  const item = installed.find((candidate) => candidate.id === memoryId);
  const previous = item?.enabled;
  if (item) {
    item.enabled = !item.enabled;
    render();
  }
  try {
    await requireOk(await api().toggle(memoryId));
    await populateSkillsTab();
  } catch (error: unknown) {
    if (item && installed.includes(item)) {
      item.enabled = previous;
      render();
    }
    toast(translate('skills.toggleFailed', { err: errorMessage(error) }), 'error');
  }
}

function fileIcon(kind?: string): string {
  const path = ({
    skill: '<path d="M11.5 2.3a.53.53 0 0 1 1 0l2.3 4.7a2.1 2.1 0 0 0 1.6 1.1l5.2.8a.53.53 0 0 1 .3.9l-3.8 3.6a2.1 2.1 0 0 0-.6 1.9l.9 5.1a.53.53 0 0 1-.8.6L13 18.6a2.1 2.1 0 0 0-2 0L6.4 21a.53.53 0 0 1-.8-.6l.9-5.1a2.1 2.1 0 0 0-.6-1.9L2.2 9.8a.53.53 0 0 1 .3-.9l5.2-.8A2.1 2.1 0 0 0 9.2 7z"/>',
    doc: '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8l6 6v12a2 2 0 0 1-2 2z"/><path d="M14 2v6h6M8 13h8M8 17h8"/>',
    script: '<circle cx="12" cy="12" r="3"/><path d="M19 13.5v-3l2-1-2-3-2 1a8 8 0 0 0-2.5-1.5L14 3h-4l-.5 3A8 8 0 0 0 7 7.5l-2-1-2 3 2 1v3l-2 1 2 3 2-1A8 8 0 0 0 9.5 18l.5 3h4l.5-3a8 8 0 0 0 2.5-1.5l2 1 2-3z"/>',
    config: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.1-3.1a6 6 0 0 1-8.3 7.3l-7.9 7.9a1 1 0 0 1-3-3l7.9-7.9a6 6 0 0 1 7.2-8.3z"/>',
    asset: '<path d="m16 6-8.4 8.6a2 2 0 0 0 2.8 2.8L18.8 8.8a4 4 0 1 0-5.6-5.6l-8.4 8.5a6 6 0 1 0 8.5 8.5l8.4-8.5"/>',
  } as Record<string, string>)[kind || ''];
  return path
    ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`
    : '·';
}

export function formatSize(value: number): string {
  const size = Number.isFinite(value) ? value : 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export async function viewFiles(memoryId: string): Promise<void> {
  const overlay = document.getElementById('skillsFilesOverlay');
  const title = document.getElementById('skillsFilesTitle');
  const description = document.getElementById('skillsFilesDesc');
  const list = document.getElementById('skillsFilesList');
  if (!(overlay instanceof HTMLElement) || !title || !description || !list) return;
  const generation = ++filesGeneration;
  title.textContent = memoryId;
  description.textContent = translate('skills.filesLoading');
  list.replaceChildren();
  overlay.style.display = 'flex';
  try {
    const result = await api().files(memoryId);
    if (generation !== filesGeneration || overlay.style.display === 'none') return;
    if (!result) {
      description.textContent = translate('skills.filesLoadFailed');
      return;
    }
    const files = Array.isArray(result.files) ? result.files : [];
    description.textContent = translate('skills.filesCount', {
      n: result.count ?? files.length,
      root: result.root || '',
    });
    list.innerHTML = files.map((file) => `<div class="skills-file-row${file.kind === 'skill' ? ' is-skill' : ''}">
      <span class="skills-file-kind">${fileIcon(file.kind)}</span>
      <span class="skills-file-path" title="${escape(file.path)}">${escape(file.path)}</span>
      <span class="skills-file-size">${escape(formatSize(file.size))}</span>
    </div>`).join('');
  } catch (error: unknown) {
    if (generation === filesGeneration) {
      description.textContent = translate('skills.filesError', { err: errorMessage(error) });
    }
  }
}

export function closeFiles(event?: Event): void {
  const overlay = document.getElementById('skillsFilesOverlay');
  if (!(overlay instanceof HTMLElement)) return;
  if (event && event.target !== overlay) return;
  filesGeneration += 1;
  overlay.style.display = 'none';
}

function onPanelClick(event: Event): void {
  const target = event.target instanceof Element
    ? event.target.closest<HTMLElement>('[data-skills-action]')
    : null;
  if (!target || target.hasAttribute('disabled')) return;
  const action = target.dataset.skillsAction;
  const skillId = target.dataset.skillId || '';
  const envName = target.dataset.envName || '';
  const button = target instanceof HTMLButtonElement ? target : null;
  if (action === 'category') setCategory(target.dataset.category || 'all');
  else if (action === 'page') setPage(Number(target.dataset.page || 1));
  else if (action === 'install') {
    void catalogInstall(
      skillId,
      button,
      target.dataset.sourceRevision || '',
      target.dataset.overwrite === 'true',
    );
  }
  else if (action === 'uninstall') void uninstallSkill(skillId);
  else if (action === 'toggle') void toggleEnabled(skillId, button);
  else if (action === 'move') {
    void moveScope(skillId, target.dataset.targetScope === 'project' ? 'project' : 'global', button);
  } else if (action === 'files') void viewFiles(skillId);
  else if (action === 'env-edit') toggleEnvEditor(skillId, envName);
  else if (action === 'env-save') void saveEnv(skillId, envName);
  else if (action === 'env-delete') void deleteEnv(skillId, envName);
}

const bridge = globals();
bridge._populateSkillsTab = populateSkillsTab;
bridge._skillsSetScope = setScope;
bridge._skillsFilter = filterSkills;
bridge._skillsSetCategory = setCategory;
bridge._skillsSetPage = setPage;
bridge._skillsRender = render;
bridge._skillsEnvToggleEditor = toggleEnvEditor;
bridge._skillsEnvSave = saveEnv;
bridge._skillsEnvDelete = deleteEnv;
bridge._skillsMoveScope = moveScope;
bridge._skillsCatalogInstall = catalogInstall;
bridge._skillsUninstall = uninstallSkill;
bridge._skillsToggleEnabled = toggleEnabled;
bridge._skillsViewFiles = viewFiles;
bridge._skillsCloseFiles = closeFiles;
bridge._destroySkills = destroySkills;

window.addEventListener('tofu:language-change', () => {
  const grid = document.getElementById('skillsCatalogGrid');
  if (grid && grid.offsetParent !== null) render();
});
