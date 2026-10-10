/*
 * Settings → 数据库 tab owner.
 *
 * Renders the storage authority surface: engine status + backend switch
 * (persisted server-side, applied by the shared approval-gated restart
 * flow) and storage location controls (accelerated write front, dual-path
 * verified backup). All heavy work runs server-side as polled jobs; this
 * module only renders state and issues bounded API calls.
 */

import { featureRegistry } from '../../feature-registry';
import { createButton, createChoice, createDialog, createField } from '../../ui';

interface FsInfo {
  type?: string | null;
  class?: string | null;
  persistence?: string | null;
  freeBytes?: number | null;
  probeLatencyMs?: number | null;
}

interface EngineInfo {
  backend: string;
  deploymentMode?: string;
  ready: boolean;
  state: string;
  lastError?: string;
  dataRoot?: string;
  authorityPath?: string;
  dataBytes?: number | null;
  filesystem?: FsInfo;
  engines?: {
    sqlite?: { available?: boolean | null; version?: string; hasData?: boolean };
    tofudb?: { available?: boolean | null; binaryPath?: string; hasData?: boolean };
  };
}

interface FastpathInfo {
  supported: boolean;
  mode: string;
  enabled: boolean;
  directory: string; // Saved next-launch configuration, never an active witness.
  kind: 'native' | 'sqlite' | 'unavailable';
  active: boolean | null;
  activeDirectory: string;
  restartRequired: boolean;
  locked: boolean;
  lockReason: string;
  authorityPath: string;
  shadowPath: string;
}

interface SearchProjectionInfo {
  required: true;
  directory: string;
  configuredDirectory: string;
  defaultDirectory: string;
  pendingDirectory: string;
  restartRequired: boolean;
}

type PathSettings =
  | { fastPath: { enabled: boolean; directory?: string } }
  | { searchProjection: { directory: string } };

interface BackendWitness {
  available?: boolean | null;
  hasData?: boolean;
  dataBytes?: number;
  authorityPath?: string;
}

interface JobInfo {
  id?: string;
  trigger?: string;
  state?: string;
  phase?: string;
  startedAt?: number;
  finishedAt?: number;
  error?: string;
  truncated?: boolean;
  resumeSupported?: boolean;
}

interface Preferences {
  backup: { enabled: boolean; directory: string };
  lastBackup?: {
    ok?: boolean; at?: number; bytes?: number; directory?: string; error?: string;
  } | null;
}

interface DbStatus {
  engine: EngineInfo;
  fastpath: FastpathInfo;
  searchProjection: SearchProjectionInfo | null;
  backends: {
    current?: string;
    sqlite?: BackendWitness;
    tofudb?: BackendWitness;
  };
  preferences: Preferences;
  migration: { job?: JobInfo | null };
}

interface PathCandidate {
  path: string;
  writable: boolean;
  storageClass: string;
  filesystemType?: string;
  persistence: string;
  freeBytes?: number | null;
  latencyMs?: number | null;
  volatile: boolean;
  isCurrent: boolean;
  recommended: boolean;
  limitations?: string[];
}

interface PathsReport {
  probedAt?: number;
  candidates?: PathCandidate[];
  recommendedPath?: string;
}

interface DatabasePanelApi {
  status(): Promise<DbStatus | null>;
  paths(refresh?: boolean): Promise<PathsReport | null>;
  pathsApply(payload: PathSettings): Promise<{ restartRequired?: boolean } | null>;
  preferences(payload: {
    backup?: { enabled?: boolean; directory?: string };
  }): Promise<{ preferences?: Preferences } | null>;
  backendSwitch(payload: { backend: string; migrate?: boolean }): Promise<{
    ok?: boolean; restartRequired?: boolean; migration?: string; notices?: string[];
  } | null>;
  migration(): Promise<{ job?: JobInfo | null } | null>;
  backupNow(payload: { directory?: string }): Promise<unknown>;
}

interface DatabaseWindow {
  Api?: {
    databasePanel?: DatabasePanelApi;
  };
  t?: (key: string, values?: Record<string, unknown>) => string;
  showConfirm?: (message: string, options?: { danger?: boolean }) => Promise<boolean>;
  showToast?: (message: string, options?: Record<string, unknown>) => void;
  restartServer?: () => void;
  _populateDatabaseTab?: () => void;
  _destroyDatabaseTab?: () => void;
  _dbSelectEngine?: (backend: string) => void;
  _dbConfirmSwitch?: () => Promise<void>;
  _dbRefreshPaths?: () => Promise<void>;
  _dbFastPathChanged?: () => void;
  _dbFastPathCustomApply?: () => void;
  _dbBackupChanged?: () => void;
  _dbBackupCustomApply?: () => void;
  _dbBackupNow?: () => Promise<void>;
}

function globals(): DatabaseWindow {
  return featureRegistry as unknown as DatabaseWindow;
}

function t(key: string, values?: Record<string, unknown>): string {
  const translate = globals().t;
  return translate ? translate(key, values) : key;
}

function toast(message: string): void {
  globals().showToast?.(message, { variant: 'info' });
}

function el<T extends HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}

function fmtBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function fmtTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return '—';
  try {
    return new Date(epochSeconds * 1000).toLocaleString();
  } catch {
    return '—';
  }
}

function fsClassLabel(storageClass: string | null | undefined): string {
  switch (storageClass) {
    case 'local-block': return t('settings.dbFsLocal');
    case 'network-filesystem': return t('settings.dbFsNetwork');
    case 'memory-filesystem': return t('settings.dbFsMemory');
    case 'container-overlay': return t('settings.dbFsOverlay');
    case 'userspace-filesystem': return t('settings.dbFsNetwork');
    default: return t('settings.dbFsUnknown');
  }
}

// ── Module state ─────────────────────────────────────────────────────────
let _status: DbStatus | null = null;
let _selectedBackend: string | null = null;
let _pathsReport: PathsReport | null = null;

// A checked picker is an editable draft until a directory is selected. A status
// refresh must not restore the saved checkbox while the user is choosing it.
let _fastPathDraft: boolean | null = null;
let _backupDraft: boolean | null = null;

let _projectionDraft: string | null = null;
let _fastDirectoryDraft: string | null = null;
let _backupDirectoryDraft: string | null = null;
let _controlsInitialized = false;

let _statusRefreshing: { promise: Promise<void>; version: number; lifecycle: number } | null = null;

function cancelDraft(section: 'fastpath' | 'backup' | 'projection'): void {
  if (!canEditPaths()) return;
  if (section === 'fastpath') { _fastPathDraft = null; _fastDirectoryDraft = null; }
  if (section === 'backup') { _backupDraft = null; _backupDirectoryDraft = null; }
  if (section === 'projection') _projectionDraft = null;
  renderPaths();
}

function initializePanelControls(): void {
  if (_controlsInitialized) return;
  _controlsInitialized = true;
  const button = (host: string, id: string, key: string, onClick: () => void) => {
    const control = createButton({ label: t(key), size: 'sm', onClick });
    control.id = id; control.dataset.i18n = key; el(host)?.append(control);
  };
  button('dbRefreshControls', 'dbPathRefresh', 'settings.dbRefreshStatus', () => { void reloadDatabase(false); });
  button('dbScanControls', 'dbPathScan', 'settings.dbScanPaths', () => { void refreshPaths(true); });
  button('dbSwitchControls', 'dbSwitchBtn', 'settings.dbSwitchConfirm', () => { void confirmSwitch(); });
  for (const backend of ['tofudb', 'sqlite']) {
    const option = createButton({ label: engineName(backend), onClick: () => selectEngine(backend) });
    option.id = backend === 'tofudb' ? 'dbEngineTofudb' : 'dbEngineSqlite';

    option.classList.add('db-engine-option');
    const state = document.createElement('span'); state.id = backend === 'tofudb' ? 'dbTofudbState' : 'dbSqliteState';
    option.append(state); el('dbEngineOptions')?.append(option);
  }
  const toggle = createChoice({ type: 'switch', label: '', disabled: true, onChange: backupChanged });
  toggle.input.id = 'dbBackupToggle'; el('dbBackupSwitch')?.append(toggle.el);
  const field = createField({ label: t('settings.dbDualBackup'), controlId: 'dbBackupCustom' });
  el('dbBackupControls')?.append(field.el);
  button('dbBackupControls', 'dbBackupApply', 'settings.dbApply', backupCustomApply);
  button('dbBackupNowControls', 'dbBackupNowBtn', 'settings.dbBackupNow', () => { void backupNow(); });
  for (const [section, prefix] of [['fastpath', 'dbFastPath'], ['backup', 'dbBackup'], ['projection', 'dbSearchProjection']] as const) {
    button(prefix + 'Controls', prefix + 'Cancel', 'common.cancel', () => cancelDraft(section));
    const input = el<HTMLInputElement>(prefix + 'Custom');
    input?.addEventListener('input', () => {
      if (!canEditPaths()) return;
      if (section === 'fastpath') _fastDirectoryDraft = input.value;
      if (section === 'backup') _backupDirectoryDraft = input.value;
      if (section === 'projection') _projectionDraft = input.value;
    });
    el<HTMLDetailsElement>(prefix + 'Disclosure')?.addEventListener('toggle', () => {
      if (el<HTMLDetailsElement>(prefix + 'Disclosure')?.open && _statusValid && !_pathsReport) void refreshPaths(false);
      renderPaths();
    });
  }
}
let _pathsSaving = false;
let _pathsRefreshing: Promise<void> | null = null;
let _statusRequestVersion = 0;
let _pathError = '';
let _migrationPoll: number | null = null;

let _mounted = false;
let _lifecycleVersion = 0;
let _statusValid = false;
let _statusLoading = false;
let _confirmingFastPath = false;

function currentLifecycle(version: number): boolean {
  return _mounted && version === _lifecycleVersion;
}

function canEditPaths(): boolean {
  return _mounted && _statusValid && !_pathsSaving && !_confirmingFastPath;
}

function api(): DatabasePanelApi | null {
  return globals().Api?.databasePanel ?? null;
}

// ── Engine card ──────────────────────────────────────────────────────────
function renderEngineCard(): void {
  if (!_status) return;
  const engine = _status.engine;
  const current = engine.backend;

  const name = el('dbEngineName'); if (name) name.textContent = engineName(current);
  const statePill = el('dbEngineState');
  if (statePill) {
    statePill.textContent = engine.ready
      ? t('settings.dbStateReady')
      : (engine.state || t('settings.dbStateDown'));
    statePill.classList.toggle('db-state-pill--ok', engine.ready);
    statePill.classList.toggle('db-state-pill--bad', !engine.ready);
  }
  const options: Array<[string, string, BackendWitness | undefined, string]> = [
    ['tofudb', 'dbEngineTofudb', _status.backends?.tofudb, 'dbTofudbState'],
    ['sqlite', 'dbEngineSqlite', _status.backends?.sqlite, 'dbSqliteState'],
  ];
  for (const [backend, cardId, witness, stateId] of options) {
    const card = el(cardId);
    if (card) {
      card.classList.toggle('db-engine-option--current', backend === current);
      card.classList.toggle('db-engine-option--selected',
        _selectedBackend !== null && _selectedBackend === backend);
      card.classList.toggle('db-engine-option--unavailable',
        witness?.available === false);
    }
    const stateEl = el(stateId);
    if (stateEl) {
      const parts: string[] = [];
      if (backend === current) parts.push(t('settings.dbStateCurrent'));
      if (witness?.available === false) parts.push(t('settings.dbNotInstalled'));
      else if (witness?.available !== true) parts.push(t('settings.dbAvailabilityUnknown'));
      else if (backend !== current || !engine.ready) parts.push(t('settings.dbVerifiedAvailable'));
      if (backend !== current && witness?.hasData) {
        parts.push(t('settings.dbHasData'));
      }
      stateEl.textContent = parts.join(' · ');
    }
  }
  const meta = el('dbEngineMeta');
  if (meta) {
    const fs = engine.filesystem || {};
    const rows = [
      [t('settings.dbFsType'), fsClassLabel(fs.class)],
      [t('settings.dbSize'), fmtBytes(engine.dataBytes)],
      [t('settings.dbFreeSpace'), fmtBytes(fs.freeBytes)],
    ];
    meta.innerHTML = '';
    for (const [label, value] of rows) {
      const item = document.createElement('div');
      item.className = 'db-meta-item';
      const labelEl = document.createElement('span');
      labelEl.className = 'db-meta-label';
      labelEl.textContent = label;
      const valueEl = document.createElement('span');
      valueEl.className = 'db-meta-value';
      valueEl.textContent = String(value);
      valueEl.title = String(value);
      item.append(labelEl, valueEl);
      meta.appendChild(item);
    }

    meta.append(pathDetail('settings.dbDataRoot', engine.dataRoot || '—'),
      pathDetail('settings.dbFastPathAuthority', engine.authorityPath || engine.dataRoot || '—'));
  }
  const switchRow = el('dbSwitchRow');
  const switchBtn = el<HTMLButtonElement>('dbSwitchBtn');
  const switchHint = el('dbSwitchHint');
  const pending = _selectedBackend !== null && _selectedBackend !== current;
  if (switchRow) switchRow.hidden = !pending;
  if (pending && switchBtn) {
    switchBtn.textContent = t('settings.dbSwitchTo', { name: engineName(_selectedBackend) });
  }
  if (pending && switchHint) {
    switchHint.textContent = switchNote(_selectedBackend as string);
  }
}

function engineName(backend: string | null): string {
  return backend === 'tofudb' ? 'TofuDB' : 'SQLite';
}

function switchNote(target: string): string {
  if (!_status) return '';
  const current = _status.engine.backend;
  if (target === 'sqlite' && current === 'tofudb' && _status.backends?.tofudb?.hasData) {
    return t('settings.dbSwitchRetainNote');
  }
  if (target === 'tofudb' && current === 'sqlite') {
    if (_status.backends?.tofudb?.hasData) return t('settings.dbSwitchReuseNote');
    if (_status.backends?.sqlite?.hasData) return t('settings.dbSwitchMigrateHint');
  }
  return t('settings.dbSwitchGenericNote');
}

function selectEngine(backend: string): void {
  if (!_statusValid || !_status) return;
  _selectedBackend = backend === _status.engine.backend ? null : backend;
  renderEngineCard();
}

async function confirmSwitch(): Promise<void> {
  const target = _selectedBackend;
  const client = api();
  if (!target || !_status || !_statusValid || !client) return;
  const lifecycle = _lifecycleVersion;
  const current = _status.engine.backend;

  let migrate = false;
  const canMigrate = target === 'tofudb' && current === 'sqlite'
    && !!_status.backends?.sqlite?.hasData && !_status.backends?.tofudb?.hasData;
  {
    const body = document.createElement('div');
    const note = document.createElement('p');
    note.textContent = switchNote(target);
    body.appendChild(note);
    let migrateInput: HTMLInputElement | null = null;
    if (canMigrate) {
      const choice = createChoice({ label: t('settings.dbSwitchMigrate'), checked: true });
      migrateInput = choice.input;
      body.appendChild(choice.el);
    }
    const dialog = createDialog({
      title: t('settings.dbSwitchTo', { name: engineName(target) }),
      body,
      actions: [
        { id: 'cancel', label: t('common.cancel'), variant: 'secondary' },
        { id: 'switch', label: t('settings.dbSwitchConfirm'), variant: 'primary' },
      ],
    });
    const choice = await dialog.open();
    if (choice !== 'switch' || !currentLifecycle(lifecycle)) return;
    migrate = !!migrateInput?.checked;
  }

  try {
    const result = await client.backendSwitch({ backend: target, migrate });

    if (!currentLifecycle(lifecycle)) return;
    if (result?.migration === 'running') {
      _selectedBackend = null;
      renderEngineCard();
      showMigration(true);
      startMigrationPoll();
      return;
    }
    if (result?.restartRequired) {
      await promptRestart();
      await refreshStatus();
    }
  } catch (error) {
    toast(String((error as { body?: { error?: string } })?.body?.error
      || (error as Error).message || error));
  }
}

async function promptRestart(): Promise<void> {

  const lifecycle = _lifecycleVersion;
  const bridge = globals();
  const confirmed = bridge.showConfirm
    ? await bridge.showConfirm(t('settings.dbRestartBody'), {})
    : false;
  if (confirmed && currentLifecycle(lifecycle)) bridge.restartServer?.();
}

// ── Migration ────────────────────────────────────────────────────────────
function showMigration(show: boolean): void {
  const box = el('dbMigration');
  if (box) box.hidden = !show;
}

function renderMigration(job: JobInfo | null | undefined): void {
  if (!job || job.state === 'done') {
    if (job?.state === 'done') {
      showMigration(false);
    }
    return;
  }
  if (job.state === 'failed') {
    showMigration(true);
    const label = el('dbMigrationLabel');
    if (label) {
      label.textContent = t('settings.dbMigrationFailed', { error: job.error || '' });
    }
    return;
  }
  showMigration(true);
  const label = el('dbMigrationLabel');
  if (label) {
    label.textContent = migrationPhaseLabel(job.phase || 'prepare');
  }
}

function migrationPhaseLabel(phase: string): string {
  switch (phase) {
    case 'snapshot': return t('settings.dbPhaseSnapshot');
    case 'init': return t('settings.dbPhaseInit');
    case 'import': return t('settings.dbPhaseImport');
    case 'switch': return t('settings.dbPhaseSwitch');
    default: return t('settings.dbPhasePrepare');
  }
}

function startMigrationPoll(): void {
  if (_migrationPoll !== null) return;

  const lifecycle = _lifecycleVersion;
  _migrationPoll = window.setInterval(() => {
    void (async () => {
      const client = api();
      if (!client) return;
      let state: { job?: JobInfo | null } | null;
      try { state = await client.migration(); }
      catch (error) {
        if (currentLifecycle(lifecycle)) reportPathError(error);
        return;
      }
      if (!currentLifecycle(lifecycle)) return;
      const job = state?.job ?? null;
      renderMigration(job);
      if (!job || job.state !== 'running') {
        stopMigrationPoll();
        if (job?.state === 'done') {
          toast(t('settings.dbMigrationDone'));
          await promptRestart();
        }
        await refreshStatus();
      }
    })();
  }, 3000);
}

function stopMigrationPoll(): void {
  if (_migrationPoll !== null) {
    window.clearInterval(_migrationPoll);
    _migrationPoll = null;
  }
}

// ── Storage location card ────────────────────────────────────────────────
// Discovery starts only when an editor is opened or Scan is requested. A
// lifecycle-owned report is shared by the independent section drafts; selecting
// a candidate never writes configuration. Apply is the persistence boundary.

function candidateMeta(candidate: PathCandidate): string {
  const free = fmtBytes(candidate.freeBytes);
  const latency = candidate.latencyMs !== null && candidate.latencyMs !== undefined
    ? `${candidate.latencyMs.toFixed(1)} ms` : '—';
  const parts = [fsClassLabel(candidate.storageClass), latency, free];
  if (candidate.volatile) parts.push(t('settings.dbVolatileTag'));
  if (candidate.isCurrent) parts.push(t('settings.dbStateCurrent'));
  return parts.join(' · ');
}

function renderCandidateCards(
  container: HTMLElement,
  { selectedPath, rebuildable = false, persistentLocal = false, onPick }: {
    selectedPath: string;
    rebuildable?: boolean;
    persistentLocal?: boolean;
    onPick: (path: string) => void;
  },
): void {
  container.replaceChildren();
  if (!_pathsReport) return;
  const candidates = (_pathsReport?.candidates || []).filter(
    (candidate) => candidate.writable && !candidate.isCurrent
      && (!persistentLocal || (candidate.storageClass === 'local-block'
        && candidate.persistence === 'persistent'))
      && (rebuildable || (!candidate.volatile
        && candidate.storageClass !== 'memory-filesystem'
        && candidate.persistence !== 'volatile'
        && candidate.persistence !== 'ephemeral')));
  if (candidates.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'db-candidates-empty';
    empty.textContent = t('settings.dbNoCandidates');
    container.appendChild(empty);
    return;
  }
  for (const candidate of candidates) {
    const selected = candidate.path === selectedPath;
    const card = createButton({
      label: '', disabled: !canEditPaths(),
      onClick: () => { if (canEditPaths()) onPick(candidate.path); },
    });
    card.classList.add('db-candidate');
    card.classList.toggle('db-candidate--selected', selected);
    card.setAttribute('aria-pressed', String(selected));
    const main = document.createElement('span');
    main.className = 'db-candidate-main';
    const pathEl = document.createElement('span');
    pathEl.className = 'db-candidate-path';
    pathEl.textContent = candidate.path;
    pathEl.title = candidate.path;
    const metaEl = document.createElement('span');
    metaEl.className = 'db-candidate-meta';
    metaEl.textContent = candidateMeta(candidate);
    main.append(pathEl, metaEl);
    card.appendChild(main);
    if (candidate.recommended) {
      const badge = document.createElement('span');
      badge.className = 'db-candidate-badge';
      badge.textContent = t('settings.dbRecommended');
      card.appendChild(badge);
    }
    container.appendChild(card);
  }
}

function searchProjection(): SearchProjectionInfo | null {
  return _status?.engine.backend === 'tofudb' && _status.searchProjection?.required
    ? _status.searchProjection : null;
}

function supportsFastPath(): boolean {
  return _statusValid && !!_status?.fastpath.supported
    && (_status.engine.backend === 'sqlite' || _status.engine.backend === 'tofudb');
}

function supportsBackup(): boolean {
  return _statusValid && (_status?.engine.backend === 'sqlite' || _status?.engine.backend === 'tofudb');
}

function pathDetail(labelKey: string, value: string): HTMLElement {
  const row = document.createElement('div');
  row.className = 'db-index-detail';
  const name = document.createElement('span');
  name.className = 'settings-field-hint';
  name.dataset.i18n = labelKey;
  name.textContent = t(labelKey);
  const path = document.createElement('div');
  path.className = 'db-path-value';
  path.textContent = value || '—';
  path.title = value;
  row.append(name, path);
  if (value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value)) {
    const copy = createButton({ label: t('settings.dbCopyPath'), size: 'sm', onClick: () => {
      const clipboard = row.ownerDocument.defaultView?.navigator.clipboard;
      if (!clipboard) { toast(t('settings.dbCopyFailed')); return; }
      const lifecycle = _lifecycleVersion;
      void clipboard.writeText(value).then(() => {
        if (currentLifecycle(lifecycle)) toast(t('settings.dbCopied'));
      }, () => { if (currentLifecycle(lifecycle)) toast(t('settings.dbCopyFailed')); });
    }});
    copy.dataset.i18n = 'settings.dbCopyPath';
    row.append(copy);
  }
  return row;
}

function initializeFastPathControls(): void {
  const switchHost = el('dbFastPathSwitch');
  if (switchHost && !switchHost.childElementCount) {
    const toggle = createChoice({ type: 'switch', label: '', disabled: true,
      onChange: () => fastPathChanged(),
    });
    toggle.input.id = 'dbFastPathToggle';

    toggle.input.setAttribute('aria-label', t('settings.dbFastPath'));
    toggle.input.dataset.i18nAriaLabel = 'settings.dbFastPath';
    switchHost.append(toggle.el);
  }
  const host = el('dbFastPathControls');
  if (!host || host.childElementCount) return;
  const field = createField({ label: t('settings.dbFastPathDirectory'), controlId: 'dbFastPathCustom' });
  field.el.querySelector('label')?.setAttribute('data-i18n', 'settings.dbFastPathDirectory');
  (field.control as HTMLInputElement).disabled = true;
  const apply = createButton({ label: t('settings.dbApply'), size: 'sm', disabled: true,
    onClick: () => fastPathCustomApply(),
  });
  apply.id = 'dbFastPathApply';
  apply.dataset.i18n = 'settings.dbApply';
  host.append(field.el, apply);
}

function initializeProjectionControls(): void {
  const host = el('dbSearchProjectionControls');
  if (!host || host.childElementCount) return;
  const field = createField({
    label: t('settings.dbIndexBase'), controlId: 'dbSearchProjectionCustom',
    hint: t('settings.dbIndexPickHint'),
  });
  const input = field.control as HTMLInputElement;
  input.placeholder = t('settings.dbIndexBasePlaceholder');

  input.dataset.i18nPlaceholder = 'settings.dbIndexBasePlaceholder';
  field.el.querySelector('label')?.setAttribute('data-i18n', 'settings.dbIndexBase');
  field.el.querySelector('.ui-field__hint')?.setAttribute('data-i18n', 'settings.dbIndexPickHint');
  const apply = createButton({ label: t('settings.dbApply'), size: 'sm',
    onClick: () => {
      const directory = input.value.trim();
      void applySearchProjection(directory);
    },
  });
  apply.id = 'dbSearchProjectionApply';

  apply.dataset.i18n = 'settings.dbApply';
  host.append(field.el, apply);
}

function renderProjection(projection: SearchProjectionInfo | null): void {
  const header = el('dbSearchProjection');
  if (header) header.hidden = !projection;

  initializeProjectionControls();
  const input = el<HTMLInputElement>('dbSearchProjectionCustom');
  if (input) { input.disabled = !projection || !canEditPaths(); input.value = _projectionDraft ?? projection?.configuredDirectory ?? ''; }
  const summary = el('dbSearchProjectionSummary');
  if (summary) {
    summary.textContent = projection ? t(projection.directory ? 'settings.dbIndexPlaced' : 'settings.dbFastPathUnknown')
      + (projection.restartRequired ? ' · ' + t('settings.dbRestartPending') : '') : '';
    summary.title = projection?.directory || '';
  }
  const apply = el<HTMLButtonElement>('dbSearchProjectionApply');
  if (apply) apply.disabled = !projection || !canEditPaths();
  const candidates = el('dbSearchProjectionCandidates');
  if (candidates && projection) renderCandidateCards(candidates, {
    selectedPath: _projectionDraft ?? projection.configuredDirectory, rebuildable: true,
    onPick: path => { _projectionDraft = path; renderPaths(); },
  });
  const detail = el('dbSearchProjectionStatus');
  if (detail) {
    detail.replaceChildren();
    if (projection) {
      detail.append(
        pathDetail('settings.dbIndexActive', projection.directory),
        pathDetail('settings.dbIndexBase', projection.configuredDirectory
          || t('settings.dbIndexAutomatic')),
        pathDetail('settings.dbIndexDefault', projection.defaultDirectory),
      );
      if (projection.restartRequired && projection.pendingDirectory !== projection.directory) {
        const pending = pathDetail('settings.dbIndexPending', projection.pendingDirectory);
        pending.classList.add('db-index-detail--pending');
        detail.appendChild(pending);
      }
    }
  }
  const actions = el('dbSearchProjectionActions');
  if (actions) {
    actions.replaceChildren();
    actions.hidden = !projection;
    if (projection) {
      const reset = createButton({
        label: t('settings.dbIndexReset'), size: 'sm',
        disabled: !canEditPaths() || !projection.configuredDirectory,
        onClick: () => { if (canEditPaths()) { _projectionDraft = ''; renderPaths(); } },
      });
      reset.id = 'dbSearchProjectionReset';
      reset.dataset.i18n = 'settings.dbIndexReset';
      actions.appendChild(reset);
    }
  }
}

function renderPaths(): void {
  if (!_mounted) return;
  const ready = _statusValid && !!_status;

  const engineCard = el('dbEngineCard');
  if (engineCard) engineCard.hidden = false;
  const pathContent = el('dbPathContent');
  if (pathContent) pathContent.hidden = false;

  el('dbPathCard')?.setAttribute('aria-busy', String(_statusLoading || _pathsSaving || _confirmingFastPath));
  const panel = el('settingsTab_database');
  const state = el('dbStatusMessage');
  if (state) {
    state.hidden = ready;
    state.textContent = t(_statusLoading ? 'settings.dbStatusLoading' : 'settings.dbStatusUnavailable');
  }
  panel?.setAttribute('aria-busy', String(_statusLoading));
  for (const id of ['dbEngineTofudb', 'dbEngineSqlite', 'dbSwitchBtn']) {
    const button = el<HTMLButtonElement>(id);
    if (button) button.disabled = !ready || _pathsSaving;
  }
  if (!ready || !_status) {
    const name = el('dbEngineName'); if (name) name.textContent = '—';
    panel?.querySelectorAll<HTMLInputElement | HTMLButtonElement>('button, input').forEach(control => {
      control.disabled = true;
    });
    for (const id of ['dbFastPathToggle', 'dbBackupToggle']) {
      const input = el<HTMLInputElement>(id);
      if (input) input.checked = false;
    }
    for (const id of ['dbFastPathField', 'dbBackupField', 'dbSearchProjection', 'dbSwitchRow']) {
      const node = el(id);
      if (node) node.hidden = true;
    }
    for (const id of ['dbEngineMeta', 'dbPathCurrent', 'dbFastPathStatus', 'dbEngineState', 'dbFastPathDetail', 'dbFastPathLockSummary', 'dbSearchProjectionSummary', 'dbBackupSummary']) {
      el(id)?.replaceChildren();
    }
    const retry = el<HTMLButtonElement>('dbPathRefresh');
    if (retry) retry.disabled = _statusLoading;
    const error = el('dbPathError');
    if (error) { error.hidden = !_pathError; error.textContent = _pathError; }
    return;
  }
  const current = el('dbPathCurrent');
  if (current) {
    const fs = _status.engine.filesystem || {};
    current.innerHTML = '';
    const subEl = document.createElement('div');
    subEl.className = 'settings-field-hint';
    subEl.textContent = `${fsClassLabel(fs.class)} · ${t('settings.dbFreeSpace')}: ${
      fmtBytes(fs.freeBytes)}`;
    current.append(subEl);
  }

  const projection = searchProjection();
  const fastpath = _status.fastpath;

  for (const id of ['dbFastPathCancel', 'dbBackupCancel', 'dbSearchProjectionCancel', 'dbPathScan']) {
    const control = el<HTMLButtonElement>(id); if (control) control.disabled = !canEditPaths() || (id === 'dbPathScan' && !!_pathsRefreshing);
  }
  const fastSupported = supportsFastPath();
  const fastEnabled = fastSupported && (_fastPathDraft ?? fastpath.enabled);
  const fastLocked = !!fastpath.locked;
  const showPicker = fastEnabled && !fastLocked;
  renderProjection(projection);
  const fastRow = el('dbFastPathRow');
  const fastToggle = el<HTMLInputElement>('dbFastPathToggle');
  const fastField = el('dbFastPathField');
  const fastHint = el('dbFastPathHint');
  if (fastRow) {
    fastRow.hidden = false;
    const desc = fastRow.querySelector('.settings-toggle-desc');
    if (desc instanceof HTMLElement) {
      desc.dataset.i18n = !fastSupported ? 'settings.dbPathUnsupported'
        : (_status.engine.backend === 'tofudb' ? 'settings.dbFastPathNativeDesc' : 'settings.dbFastPathDesc');
      desc.textContent = t(desc.dataset.i18n);
    }
  }
  if (fastToggle) {
    fastToggle.checked = fastEnabled;
    fastToggle.disabled = !fastSupported || fastLocked || !canEditPaths();
    fastToggle.dataset.i18nAriaLabel = 'settings.dbFastPath';
    fastToggle.setAttribute('aria-label', t('settings.dbFastPath'));
  }
  if (fastField) fastField.hidden = !fastSupported || fastLocked;
  const fastCandidates = el('dbFastPathCandidates');
  if (fastCandidates) {
    fastCandidates.replaceChildren();
    if (showPicker) renderCandidateCards(fastCandidates, {
      selectedPath: _fastDirectoryDraft ?? fastpath.directory,
      persistentLocal: _status.engine.backend === 'tofudb',
      onPick: path => { _fastDirectoryDraft = path; renderPaths(); },
    });
  }
  const fastInput = el<HTMLInputElement>('dbFastPathCustom');
  if (fastInput) {

    fastInput.value = _fastDirectoryDraft ?? fastpath.directory;
    fastInput.disabled = !showPicker || !canEditPaths();
    fastInput.dataset.i18nPlaceholder = 'settings.dbCustomPathPlaceholder';
    fastInput.placeholder = t(fastInput.dataset.i18nPlaceholder);
    fastInput.dataset.i18nAriaLabel = 'settings.dbFastPath';
    fastInput.setAttribute('aria-label', t(fastInput.dataset.i18nAriaLabel));
  }
  const fastApply = el<HTMLButtonElement>('dbFastPathApply');
  if (fastApply) fastApply.disabled = !fastSupported || fastLocked || !canEditPaths();
  if (fastHint) fastHint.textContent = fastEnabled ? t('settings.dbFastPathPickHint') : '';
  const fastStatus = el('dbFastPathDetail');
  const fastSummary = el('dbFastPathStatus');
  if (fastSummary) fastSummary.textContent = fastpath.active === true
    ? t('settings.dbFastPathOn')
    : fastpath.active === false ? t('settings.dbFastPathOff') : t('settings.dbFastPathUnknown');
  if (fastSummary && fastpath.restartRequired) fastSummary.textContent += ' · ' + t('settings.dbRestartPending');
  if (fastSummary && fastLocked) fastSummary.textContent += ' · ' + t('settings.dbFastPathLocked');

  if (fastSummary) fastSummary.title = fastSummary.textContent || '';
  const lockSummary = el('dbFastPathLockSummary');
  if (lockSummary) { lockSummary.hidden = !fastLocked; lockSummary.textContent = fastLocked ? (fastpath.lockReason || t('settings.dbFastPathLockedDefault')) + ' ' + t('settings.dbFastPathOffline') : ''; }
  if (fastStatus) {
    fastStatus.replaceChildren();
    fastStatus.append(
      pathDetail('settings.dbFastPathSaved', fastpath.enabled
        ? fastpath.directory || t('settings.dbIndexAutomatic') : t('settings.dbFastPathOff')),
      pathDetail('settings.dbFastPathActive', fastpath.active === true
        ? fastpath.activeDirectory || t('settings.dbFastPathUnknown')
        : t(fastpath.active === false ? 'settings.dbFastPathOff' : 'settings.dbFastPathUnknown')),
    );
    if (fastpath.authorityPath) fastStatus.append(pathDetail('settings.dbFastPathAuthority', fastpath.authorityPath));
    if (fastpath.shadowPath) fastStatus.append(pathDetail('settings.dbFastPathShadow', fastpath.shadowPath));
    if (fastpath.restartRequired) fastStatus.append(pathDetail('settings.dbFastPathPending', t('settings.dbRestartPending')));
    if (fastLocked) fastStatus.append(
      pathDetail('settings.dbFastPathLocked', fastpath.lockReason || t('settings.dbFastPathLockedDefault')),
      pathDetail('settings.dbFastPathOfflineTitle', t('settings.dbFastPathOffline')),
    );
  }

  const backup = _status.preferences?.backup || { enabled: false, directory: '' };
  const backupSupported = supportsBackup();
  const backupEnabled = backupSupported && (_backupDraft ?? backup.enabled);
  const backupToggle = el<HTMLInputElement>('dbBackupToggle');
  const backupField = el('dbBackupField');
  if (backupToggle) {
    backupToggle.checked = backupEnabled;
    backupToggle.disabled = !backupSupported || !canEditPaths();
    backupToggle.dataset.i18nAriaLabel = 'settings.dbDualBackup';
    backupToggle.setAttribute('aria-label', t('settings.dbDualBackup'));
  }
  const backupDesc = el('dbBackupDesc');
  if (backupDesc) {
    backupDesc.dataset.i18n = backupSupported ? 'settings.dbDualBackupDesc' : 'settings.dbPathUnsupported';
    backupDesc.textContent = t(backupDesc.dataset.i18n);
  }
  if (backupField) backupField.hidden = !backupSupported;
  const backupCandidates = el('dbBackupCandidates');
  if (backupCandidates) {
    backupCandidates.replaceChildren();
    if (backupEnabled) renderCandidateCards(backupCandidates, {
      selectedPath: _backupDirectoryDraft ?? backup.directory,
      onPick: path => { _backupDirectoryDraft = path; renderPaths(); },
    });
  }
  const backupInput = el<HTMLInputElement>('dbBackupCustom');
  if (backupInput) {

    backupInput.value = _backupDirectoryDraft ?? backup.directory;
    backupInput.disabled = !backupEnabled || !canEditPaths();
    backupInput.dataset.i18nAriaLabel = 'settings.dbDualBackup';
    backupInput.setAttribute('aria-label', t('settings.dbDualBackup'));
  }
  const backupApply = el<HTMLButtonElement>('dbBackupApply');
  if (backupApply) backupApply.disabled = !backupSupported || !canEditPaths();
  const backupSummary = el('dbBackupSummary');
  if (backupSummary) backupSummary.textContent = backup.enabled ? backup.directory : t('settings.dbFastPathOff');

  if (backupSummary) backupSummary.title = backupSummary.textContent || '';
  const backupButton = el<HTMLButtonElement>('dbBackupNowBtn');
  if (backupButton) backupButton.disabled = !backupSupported || _pathsSaving
    || !backup.enabled || !backup.directory;
  const refreshButton = el<HTMLButtonElement>('dbPathRefresh');
  if (refreshButton) refreshButton.disabled = _pathsSaving || !!_pathsRefreshing;
  el('dbPathCard')?.setAttribute('aria-busy', String(_pathsSaving || _confirmingFastPath));
  const error = el('dbPathError');
  if (error) {
    error.hidden = !_pathError;
    error.textContent = _pathError;
  }
  const backupMeta = el('dbBackupMeta');
  if (backupMeta) {
    const last = _status.preferences?.lastBackup;
    backupMeta.textContent = last?.ok
      ? t('settings.dbBackupMetaLast', {
          time: fmtTime(last.at), size: fmtBytes(last.bytes) })
      : (last?.error
          ? t('settings.dbBackupFailed', { error: last.error })
          : t('settings.dbBackupNever'));
  }
}

function reportPathError(error: unknown, saved = false): void {
  const detail = String((error as { body?: { error?: string } })?.body?.error
    || (error as Error)?.message || error);
  _pathError = t(saved ? 'settings.dbPathSavedRefreshFailed' : 'settings.dbPathFailed', {
    error: detail,
  });
  toast(_pathError);
  renderPaths();
}

async function refreshPaths(refresh: boolean): Promise<void> {
  const client = api();
  if (!client || !_mounted || _pathsSaving) return;
  const lifecycle = _lifecycleVersion;
  if (_pathsRefreshing) return _pathsRefreshing;
  _pathsRefreshing = (async () => {
    // Publish the single-flight guard before invoking even a synchronous stub.
    await Promise.resolve();
    try {
      const report = await client.paths(refresh);
      if (!currentLifecycle(lifecycle)) return;
      if (!report) throw new Error(t('settings.dbStatusUnavailable'));
      _pathsReport = report;
    } catch (error) {
      if (currentLifecycle(lifecycle)) reportPathError(error);
    } finally {
      if (currentLifecycle(lifecycle)) {
        _pathsRefreshing = null;
        renderPaths();
      }
    }
  })();
  renderPaths();
  return _pathsRefreshing;
}

async function savePathSettings(
  draft: 'fastpath' | 'backup' | 'projection',
  save: () => Promise<boolean>,
): Promise<void> {
  if (!canEditPaths()) return;
  const lifecycle = _lifecycleVersion;
  _pathsSaving = true;
  _pathError = '';
  // An earlier status read must not overwrite the acknowledged mutation.
  ++_statusRequestVersion;
  renderPaths();
  let saved = false;
  try {
    const restartRequired = await save();
    if (!currentLifecycle(lifecycle)) return;
    saved = true;
    if (draft === 'fastpath') { _fastPathDraft = null; _fastDirectoryDraft = null; }
    else if (draft === 'backup') { _backupDraft = null; _backupDirectoryDraft = null; }
    else _projectionDraft = null;
    await refreshStatus();
    if (currentLifecycle(lifecycle) && restartRequired) await promptRestart();
  } catch (error) {
    // A failed refresh after a successful write is not a failed save. Keep the
    // active/pending paths server-owned and ask the user to refresh explicitly.
    if (currentLifecycle(lifecycle)) reportPathError(error, saved);
  } finally {
    if (!currentLifecycle(lifecycle)) return;

    _pathsSaving = false;
    renderPaths();
  }
}

function fastPathChanged(): void {
  const toggle = el<HTMLInputElement>('dbFastPathToggle');
  if (!toggle) return;
  if (!canEditPaths() || !supportsFastPath() || _status?.fastpath.locked) { renderPaths(); return; }
  _fastPathDraft = toggle.checked;
  _pathError = '';
  renderPaths();
}

function fastPathCustomApply(): void {

  if ((_fastPathDraft ?? _status?.fastpath.enabled) === false) { void applyFastPath(false, ''); return; }
  if (!canEditPaths() || !supportsFastPath() || _status?.fastpath.locked) return;
  const input = el<HTMLInputElement>('dbFastPathCustom');
  const directory = (input?.value || '').trim();
  if (!directory) {
    toast(t('settings.dbCustomPathRequired'));
    return;
  }
  void applyFastPath(true, directory);
}

async function applySearchProjection(directory: string): Promise<void> {
  const client = api();
  if (!client || !searchProjection() || !canEditPaths()) return;
  await savePathSettings('projection', async () => {
    // Empty means the default, never an empty custom directory. The server
    // resolves and namespaces a base by database; the UI never guesses paths.
    const result = await client.pathsApply({ searchProjection: { directory } });

    if (!result) throw new Error(t('settings.dbStatusUnavailable'));
    return result?.restartRequired === true;
  });
}

async function applyFastPath(enabled: boolean, directory: string): Promise<void> {
  const client = api();
  if (!client || !supportsFastPath() || !canEditPaths() || _status?.fastpath.locked) return;
  const lifecycle = _lifecycleVersion;
  if (enabled && _status?.engine.backend === 'tofudb') {
    // Custom paths are also validated by the server's authoritative filesystem probe.
    const known = _pathsReport?.candidates?.find(candidate => candidate.path === directory);
    if (known && (known.volatile || known.persistence !== 'persistent'
      || known.storageClass !== 'local-block' || !known.writable)) {
      reportPathError(new Error(t('settings.dbFastPathPersistentRequired')));
      return;
    }
    _confirmingFastPath = true;
    renderPaths();
    let confirmed = false;
    try {
      confirmed = !!(await globals().showConfirm?.(t('settings.dbFastPathNativeConfirm'), { danger: true }));
    } catch (error) {
      if (currentLifecycle(lifecycle)) reportPathError(error);
    } finally {
      if (currentLifecycle(lifecycle)) { _confirmingFastPath = false; renderPaths(); }
    }
    if (!currentLifecycle(lifecycle) || !confirmed || _status?.fastpath.locked) return;
  }
  await savePathSettings('fastpath', async () => {
    const result = await client.pathsApply({ fastPath: { enabled, directory } });

    if (!result) throw new Error(t('settings.dbStatusUnavailable'));
    return result?.restartRequired === true;
  });
}

function backupChanged(): void {
  const toggle = el<HTMLInputElement>('dbBackupToggle');
  if (!toggle) return;
  if (!canEditPaths() || !supportsBackup()) { renderPaths(); return; }
  _backupDraft = toggle.checked;
  _pathError = '';
  renderPaths();
}

function backupCustomApply(): void {

  if ((_backupDraft ?? _status?.preferences.backup.enabled) === false) { void saveBackupPreference(false, undefined); return; }
  if (!canEditPaths() || !supportsBackup()) return;
  const input = el<HTMLInputElement>('dbBackupCustom');
  const directory = (input?.value || '').trim();
  if (!directory) {
    toast(t('settings.dbCustomPathRequired'));
    return;
  }
  void saveBackupPreference(true, directory);
}

async function saveBackupPreference(
  enabled: boolean,
  directory: string | undefined,
): Promise<void> {
  const client = api();
  if (!client || !supportsBackup() || _pathsSaving) return;
  await savePathSettings('backup', async () => {
    const result = await client.preferences({
      backup: { enabled, ...(directory !== undefined ? { directory } : {}) },
    });
    // The subsequent fenced status read owns all preference updates.
    if (!result) throw new Error(t('settings.dbStatusUnavailable'));
    return false;
  });
}

async function backupNow(): Promise<void> {
  const client = api();
  const backup = _status?.preferences?.backup;
  if (!client || !supportsBackup() || !backup?.enabled || !backup.directory || _pathsSaving) return;
  const lifecycle = _lifecycleVersion;
  await savePathSettings('backup', async () => {
    await client.backupNow({});
    if (currentLifecycle(lifecycle)) toast(t('settings.dbBackupDone'));
    return false;
  });
}

// ── Lifecycle ────────────────────────────────────────────────────────────
function refreshStatus(): Promise<void> {
  if (_statusRefreshing?.version === _statusRequestVersion && _statusRefreshing.lifecycle === _lifecycleVersion) return _statusRefreshing.promise;
  const promise = readStatus();
  const flight = { promise, version: _statusRequestVersion, lifecycle: _lifecycleVersion };
  _statusRefreshing = flight;
  void promise.finally(() => { if (_statusRefreshing === flight) _statusRefreshing = null; }).catch(() => {});
  return promise;
}

async function readStatus(): Promise<void> {
  const client = api();
  if (!_mounted) return;
  const lifecycle = _lifecycleVersion;
  const version = ++_statusRequestVersion;
  _statusValid = false;
  _statusLoading = true;
  renderPaths();
  let status: DbStatus | null;
  try {
    if (!client) throw new Error(t('settings.dbStatusUnavailable'));
    status = await client.status();
    if (!status) throw new Error(t('settings.dbStatusUnavailable'));
  } catch (error) {
    if (!currentLifecycle(lifecycle) || version !== _statusRequestVersion) return;
    _statusLoading = false;
    _status = null;
    renderPaths();
    throw error;
  }
  if (!currentLifecycle(lifecycle) || version !== _statusRequestVersion) return;
  _statusLoading = false;
  _statusValid = true;
  if (_status?.engine.backend !== status.engine.backend) {
    _fastPathDraft = null;
    _backupDraft = null;
    _projectionDraft = null;
    _fastDirectoryDraft = null;
    _backupDirectoryDraft = null;
    _pathsReport = null;
  }
  _status = status;
  renderEngineCard();
  renderMigration(status.migration?.job ?? null);
  renderPaths();
  if (status.migration?.job?.state === 'running') startMigrationPoll();
}

async function reloadDatabase(refresh: boolean): Promise<void> {
  const lifecycle = _lifecycleVersion;
  _pathError = '';
  try {
    await refreshStatus();
    // Entry and status refresh never discover candidates; only explicit editors/scan do.
  } catch (error) {
    if (currentLifecycle(lifecycle)) reportPathError(error);
  }
}

function populateDatabaseTab(): void {
  _mounted = true;

  initializeFastPathControls();
  initializeProjectionControls();
  initializePanelControls();
  void reloadDatabase(false);
}

function destroyDatabaseTab(): void {
  _mounted = false;

  for (const id of ['dbEngineCard', 'dbPathContent']) {
    const node = el(id);
    if (node) node.hidden = true;
  }
  const message = el('dbStatusMessage');
  if (message) {
    message.hidden = false;
    message.textContent = t('settings.dbStatusLoading');
  }
  ++_lifecycleVersion;
  ++_statusRequestVersion;
  stopMigrationPoll();
  _status = null;
  _statusValid = false;
  _statusLoading = false;
  _selectedBackend = null;
  _pathsReport = null;
  _pathsRefreshing = null;
  _pathsSaving = false;

  _statusRefreshing = null;
  _confirmingFastPath = false;
  _fastPathDraft = null;
  _backupDraft = null;
  _projectionDraft = null;
  _fastDirectoryDraft = null;
  _backupDirectoryDraft = null;
  _pathError = '';

}

const bridge = globals();
bridge._populateDatabaseTab = populateDatabaseTab;
bridge._destroyDatabaseTab = destroyDatabaseTab;
bridge._dbSelectEngine = selectEngine;
bridge._dbConfirmSwitch = confirmSwitch;
bridge._dbRefreshPaths = () => reloadDatabase(false);
bridge._dbFastPathChanged = fastPathChanged;
bridge._dbFastPathCustomApply = fastPathCustomApply;
bridge._dbBackupChanged = backupChanged;
bridge._dbBackupCustomApply = backupCustomApply;
bridge._dbBackupNow = backupNow;
