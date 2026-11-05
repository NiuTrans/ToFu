/**
 * Responsibility: the shared server-folder browser — breadcrumb navigation,
 * a bounded name filter that also jumps to typed paths, inline folder
 * creation, recoverable deletion and add-to-workspace — used by the Project
 * Co-Pilot workbench and by sidebar folder settings.
 * Entry point: createFolderBrowser.
 * Dependencies: a host-owned ProjectBrowseCoordinator (cache, cancellation,
 * response validation), UI kit factories and injected copy/transport ports.
 * Hosts own workspace membership: this pane reports add/delete intents through
 * ports and never writes host state itself.
 */
import type { ProjectBrowseCoordinator, ProjectBrowseData, ProjectBrowseDirectory } from '../../core/project-browse-coordinator';
import { createButton, createIconButton, createSpinner } from '../../ui';
import { folderIcon } from './folder-icons';

export type FolderBrowserTranslate = (key: string, values?: Record<string, string | number>) => string;

export interface FolderBrowserOptions {
  t: FolderBrowserTranslate;
  coordinator: ProjectBrowseCoordinator;
  browse: (path: string, showHidden: boolean, signal: AbortSignal | undefined) => Promise<unknown>;
  /** Raw `/project/mkdir` response. Hosts without it get no create control. */
  mkdir?: (parent: string, name: string) => Promise<unknown>;
  /** Raw `/project/rmdir` response. Hosts without it get no delete control. */
  rmdir?: (path: string) => Promise<unknown>;
  isAdded: (path: string) => boolean;
  /** Stage `path` in the host workspace. A returned message is the host's
   * refusal (e.g. a full workspace); the browser shows it in place. */
  onAdd: (path: string) => string | void;
  /** Runs when a confirmed delete is sent; a returned function restores the
   * host's state if the server refuses the delete. */
  onDeleted?: (path: string) => (() => void) | void;
  notify?: (message: string, variant: 'success' | 'error') => void;
  /** Host-owned content placed between the filter and the folder list. */
  prelude?: HTMLElement | null;
}

export interface FolderBrowserHandle {
  readonly el: HTMLElement;
  navigate(path: string): Promise<void>;
  /** Re-read the open directory from the server, bypassing the cache. */
  refresh(): Promise<void>;
  currentPath(): string;
  /** Repaint added markers after the host changed its workspace list. */
  syncAdded(): void;
  /** Abort the in-flight read; late results never repaint. */
  cancel(): void;
  /** Re-read the copy after a UI language switch. */
  relabel(): void;
  /** True while a create/delete command is in flight. */
  busy(): boolean;
  /** Resolves once in-flight create/delete commands (and any rollback) settle. */
  settled(): Promise<void>;
  dispose(): void;
}

interface FolderRow {
  dir: ProjectBrowseDirectory;
  el: HTMLElement;
  open: HTMLButtonElement;
  actions: HTMLElement;
  add: HTMLButtonElement;
  remove: HTMLButtonElement | null;
}

// Browser-owned transient input, not an invitation to retain an arbitrarily
// large pasted string for every repaint.
const FILTER_MAX_CODE_UNITS = 256;
const PATH_LIKE = /^(?:~|\/|\\\\|[A-Za-z]:[\\/])/;

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
);
const basename = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() ?? path;
const errorText = (error: unknown, fallback: string): string => (
  error instanceof Error && error.message ? error.message : fallback
);
// An IME commit (e.g. pinyin) also fires Enter; it confirms text, not an action.
const isComposing = (event: KeyboardEvent): boolean => event.isComposing || event.keyCode === 229;

function node<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text = ''): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  el.className = className;
  if (text) el.textContent = text;
  return el;
}

/** Each ancestor of `path` from its root, with the path that opens it. */
export function folderCrumbs(path: string): Array<{ label: string; path: string }> {
  const drive = /^([A-Za-z]:)(?:[\\/]|$)/.exec(path);
  if (drive) {
    const crumbs = [{ label: `${drive[1]}\\`, path: `${drive[1]}\\` }];
    let walk = drive[1];
    for (const part of path.slice(drive[0].length).split(/[\\/]/).filter(Boolean)) {
      walk = `${walk}\\${part}`;
      crumbs.push({ label: part, path: walk });
    }
    return crumbs;
  }
  const absolute = path.startsWith('/');
  const parts = path.split('/').filter(Boolean);
  const crumbs = absolute ? [{ label: '/', path: '/' }] : [];
  let walk = '';
  parts.forEach((part, index) => {
    walk = absolute || index > 0 ? `${walk}/${part}` : part;
    crumbs.push({ label: part, path: walk });
  });
  return crumbs.length ? crumbs : [{ label: path || '~', path: path || '~' }];
}

function joinPath(parent: string, name: string): string {
  const separator = parent.includes('\\') && !parent.includes('/') ? '\\' : '/';
  return `${parent.replace(/[\\/]+$/, '')}${separator}${name}`;
}

/** Settle a raw mkdir/rmdir Response. Transport unwraps v4 success into
 * `{ok: true, ...data}`; failures carry a problem `detail` or a retained
 * `{error}` envelope, and that server reason is what the user sees. */
async function commandResult(response: unknown, fallback: string): Promise<Record<string, unknown>> {
  if (!isRecord(response) || typeof response.json !== 'function') throw new Error(fallback);
  const settled = response as unknown as Response;
  const body: unknown = await settled.json().catch(() => null);
  const data = isRecord(body) ? body : {};
  if (settled.ok && data.ok === true) return data;
  const error = data.error;
  for (const reason of [data.detail, error, isRecord(error) ? error.message : null]) {
    if (typeof reason === 'string' && reason.trim()) throw new Error(reason.trim());
  }
  throw new Error(fallback);
}

export function createFolderBrowser(options: FolderBrowserOptions): FolderBrowserHandle {
  const { t, coordinator } = options;
  let disposed = false;
  let data: ProjectBrowseData | null = null;
  let requested = '';
  let showHidden = false;
  let filter = '';
  let rows: FolderRow[] = [];
  let rovingPath = '';
  let confirming: { row: FolderRow; panel: HTMLElement } | null = null;
  let creating = false;
  let createParent = '';
  // A host refusal stays visible only until a later add succeeds.
  let alertIsRefusal = false;
  const commands = new Set<Promise<unknown>>();

  const el = node('section', 'folder-browser');
  el.setAttribute('aria-label', t('pm.browseEmpty'));

  const bar = node('div', 'folder-browser-bar');
  const up = createIconButton({ icon: folderIcon('up'), label: t('pm.goUp'), size: 'sm', disabled: true,
    onClick: () => { if (data?.parent) void navigate(data.parent, true); } });
  const crumbs = node('nav', 'folder-browser-crumbs');
  crumbs.setAttribute('aria-label', t('folder.settings.browseLocation'));
  const crumbList = node('ol', 'folder-browser-crumb-list');
  crumbs.append(crumbList);
  const newFolder = createIconButton({ icon: folderIcon('folderPlus'), label: t('pm.newFolderHere'), size: 'sm',
    disabled: true, onClick: () => openCreate() });
  newFolder.hidden = !options.mkdir;
  const hiddenToggle = createIconButton({ icon: folderIcon('eye'), label: t('pm.showHidden'), size: 'sm', pressed: false,
    onClick: () => {
      showHidden = !showHidden;
      hiddenToggle.setAttribute('aria-pressed', String(showHidden));
      hiddenToggle.title = t(showHidden ? 'pm.hideHidden' : 'pm.showHidden');
      // A pending click-through is the folder the reader asked for.
      void navigate(requested || data?.path || '~');
    } });
  bar.append(up, crumbs, newFolder, hiddenToggle);

  const search = node('div', 'folder-browser-search');
  const searchIcon = folderIcon('search');
  searchIcon.classList.add('folder-browser-search-icon');
  const filterInput = node('input', 'folder-browser-input folder-browser-filter');
  filterInput.type = 'search';
  filterInput.placeholder = t('pm.browseSearchPlaceholder');
  filterInput.setAttribute('aria-label', t('pm.browseSearchPlaceholder'));
  filterInput.autocomplete = 'off';
  filterInput.spellcheck = false;
  filterInput.enterKeyHint = 'go';
  const clearFilter = createIconButton({ icon: folderIcon('close'), label: t('pm.browseClearSearch'), size: 'sm',
    onClick: () => { setFilter(''); filterInput.focus(); } });
  clearFilter.hidden = true;
  search.append(searchIcon, filterInput, clearFilter);

  // Scroll region: alert, typed-path jump, inline create, rows, status, note.
  const list = node('div', 'folder-browser-list');
  list.tabIndex = -1;
  const alert = node('div', 'folder-browser-alert');
  alert.setAttribute('role', 'alert');
  alert.hidden = true;
  const jump = node('button', 'folder-browser-jump');
  jump.type = 'button';
  jump.hidden = true;
  const jumpPath = node('span', 'folder-browser-jump-path');
  const jumpLabel = node('span', 'folder-browser-jump-label', t('folder.settings.go'));
  jump.append(folderIcon('locate'), jumpLabel, jumpPath);
  jump.addEventListener('click', () => jumpToTypedPath());

  const create = node('div', 'folder-browser-create');
  create.hidden = true;
  const createInput = node('input', 'folder-browser-input folder-browser-create-input');
  createInput.type = 'text';
  createInput.placeholder = t('folder.namePh');
  createInput.autocomplete = 'off';
  createInput.spellcheck = false;
  const createSubmit = createButton({ label: t('folder.create'), size: 'sm', variant: 'primary', onClick: () => { void submitCreate(); } });
  const createCancel = createButton({ label: t('common.cancel'), size: 'sm', variant: 'ghost', onClick: () => closeCreate(true) });
  const createError = node('p', 'folder-browser-create-error');
  createError.setAttribute('role', 'alert');
  createError.hidden = true;
  const createIcon = folderIcon('folderPlus');
  createIcon.classList.add('folder-browser-icon');
  create.append(createIcon, createInput, createSubmit, createCancel, createError);

  const rowsEl = node('div', 'folder-browser-rows');
  rowsEl.setAttribute('role', 'list');
  const status = node('div', 'folder-browser-state');
  status.setAttribute('role', 'status');
  const note = node('p', 'folder-browser-note');
  note.hidden = true;
  list.append(alert, jump, create, rowsEl, status, note);

  const footer = node('div', 'folder-browser-footer');
  const current = node('span', 'folder-browser-current');
  const currentText = document.createElement('bdi');
  currentText.dir = 'ltr';
  current.append(currentText);
  const addCurrent = createButton({ label: t('pm.addThisFolder'), size: 'sm', title: t('pm.addThisFolderTitle'), disabled: true,
    onClick: () => {
      // The button disables once added; keep keyboard focus inside the pane.
      if (data && addFolder(data.path) && addCurrent.disabled) focusRow(visibleRows()[0]);
    } });
  footer.append(current, addCurrent);

  el.append(bar, search);
  if (options.prelude) {
    const prelude = node('div', 'folder-browser-prelude');
    prelude.append(options.prelude);
    el.append(prelude);
  }
  el.append(list, footer);

  function labelButton(button: HTMLButtonElement, text: string): void {
    button.setAttribute('aria-label', text);
    button.title = text;
  }

  // Static copy; rows, footer and states re-read theirs when repainted.
  function applyCopy(): void {
    el.setAttribute('aria-label', t('pm.browseEmpty'));
    labelButton(up, t('pm.goUp'));
    crumbs.setAttribute('aria-label', t('folder.settings.browseLocation'));
    labelButton(newFolder, t('pm.newFolderHere'));
    hiddenToggle.setAttribute('aria-label', t('pm.showHidden'));
    hiddenToggle.title = t(showHidden ? 'pm.hideHidden' : 'pm.showHidden');
    filterInput.placeholder = t('pm.browseSearchPlaceholder');
    filterInput.setAttribute('aria-label', t('pm.browseSearchPlaceholder'));
    labelButton(clearFilter, t('pm.browseClearSearch'));
    jumpLabel.textContent = t('folder.settings.go');
    createInput.placeholder = t('folder.namePh');
    createSubmit.textContent = t('folder.create');
    createCancel.textContent = t('common.cancel');
    addCurrent.title = t('pm.addThisFolderTitle');
  }

  /** Track a create/delete server command (and any rollback) so a host can
   * wait for its effects before reading its own state. Follow-up reads are
   * deliberately outside: a save must not wait on a directory listing. */
  function track<T>(command: Promise<T>): Promise<T> {
    commands.add(command);
    void command.finally(() => commands.delete(command));
    return command;
  }

  function setStatus(message: string, loading = false): void {
    status.replaceChildren();
    if (loading) {
      // The adjacent text is this region's announcement; the spinner is decoration.
      const spinner = createSpinner({ size: 'sm', label: message });
      spinner.setAttribute('aria-hidden', 'true');
      status.append(spinner);
    }
    if (message) status.append(node('span', '', message));
  }

  function showAlert(message: string, retryPath?: string): void {
    alertIsRefusal = false;
    alert.replaceChildren(node('span', 'folder-browser-alert-text', message));
    if (retryPath !== undefined) {
      alert.append(createButton({ label: t('folder.settings.retry'), size: 'sm', onClick: () => { void navigate(retryPath, true); } }));
    }
    alert.hidden = false;
  }

  function renderCrumbs(path: string): void {
    const trail = folderCrumbs(path);
    crumbList.replaceChildren(...trail.map((crumb, index) => {
      const item = node('li', 'folder-browser-crumb-item');
      if (/[\\/]$/.test(crumb.label)) item.dataset.root = 'true';
      const last = index === trail.length - 1;
      const control = last ? node('span', 'folder-browser-crumb', crumb.label) : node('button', 'folder-browser-crumb', crumb.label);
      control.title = crumb.path;
      if (last) control.setAttribute('aria-current', 'location');
      else {
        (control as HTMLButtonElement).type = 'button';
        control.addEventListener('click', () => { void navigate(crumb.path, true); });
      }
      item.append(control);
      return item;
    }));
    crumbs.scrollLeft = crumbs.scrollWidth;
    markClippedCrumbs();
  }

  function markClippedCrumbs(): void {
    crumbs.toggleAttribute('data-clipped', crumbs.scrollLeft > 0);
  }

  function paintAdded(row: FolderRow): void {
    const added = options.isAdded(row.dir.path);
    row.el.dataset.added = String(added);
    const label = added ? t('folder.settings.rootAdded', { path: row.dir.name }) : `${t('pm.addToWorkspace')}: ${row.dir.name}`;
    row.add.replaceChildren(folderIcon(added ? 'check' : 'plus'));
    row.add.setAttribute('aria-label', label);
    row.add.title = label;
    if (added) row.add.setAttribute('aria-disabled', 'true');
    else row.add.removeAttribute('aria-disabled');
  }

  function renderFooter(): void {
    const path = data?.path ?? '';
    currentText.textContent = path;
    current.title = path;
    const added = !!path && options.isAdded(path);
    addCurrent.disabled = !path || added;
    addCurrent.replaceChildren(folderIcon(added ? 'check' : 'plus'),
      document.createTextNode(added ? t('folder.settings.rootAdded', { path: basename(path) }) : t('pm.addThisFolder')));
  }

  function buildRow(dir: ProjectBrowseDirectory): FolderRow {
    const el = node('div', 'folder-browser-row');
    el.setAttribute('role', 'listitem');
    el.dataset.dirPath = dir.path;
    if (dir.hidden) el.dataset.hiddenDir = 'true';
    if (dir.hasCode) el.dataset.code = 'true';
    const open = node('button', 'folder-browser-open');
    open.type = 'button';
    open.title = t('pm.openTitle', { name: dir.name });
    const icon = folderIcon(dir.hasCode ? 'folderCode' : 'folder');
    icon.classList.add('folder-browser-icon');
    open.append(icon, node('span', 'folder-browser-name', dir.name));
    if (dir.hasCode) open.append(node('span', 'folder-browser-badge', t('pm.codeBadge')));
    if (dir.itemCount > 0) open.append(node('span', 'folder-browser-count', dir.itemCount > 100 ? '100+' : String(dir.itemCount)));
    const chevron = folderIcon('chevron');
    chevron.classList.add('folder-browser-chevron');
    open.append(chevron);
    const actions = node('div', 'folder-browser-row-actions');
    const add = createIconButton({ icon: folderIcon('plus'), label: t('pm.addToWorkspace'), size: 'sm' });
    add.dataset.action = 'add';
    const row: FolderRow = { dir, el, open, actions, add, remove: null };
    if (options.rmdir) {
      row.remove = createIconButton({ icon: folderIcon('trash'), label: `${t('pm.deleteFolder')}: ${dir.name}`, size: 'sm',
        onClick: () => openConfirm(row) });
      row.remove.dataset.action = 'delete';
      actions.append(row.remove);
    }
    actions.append(add);
    open.addEventListener('click', () => { void navigate(dir.path, true); });
    add.addEventListener('click', () => { addFolder(dir.path); });
    el.append(open, actions);
    paintAdded(row);
    return row;
  }

  function visibleRows(): FolderRow[] {
    return rows.filter(row => !row.el.hidden);
  }

  function setRoving(target: FolderRow | null): void {
    rovingPath = target?.dir.path ?? '';
    for (const row of rows) {
      const tabIndex = row === target ? 0 : -1;
      row.open.tabIndex = tabIndex;
      row.add.tabIndex = tabIndex;
      if (row.remove) row.remove.tabIndex = tabIndex;
    }
  }

  function focusRow(row: FolderRow | undefined): void {
    if (!row) { list.focus(); return; }
    setRoving(row);
    row.open.focus();
    row.el.scrollIntoView?.({ block: 'nearest' });
  }

  function applyFilter(): void {
    const raw = filter.trim();
    const typedPath = PATH_LIKE.test(raw) ? raw : '';
    jump.hidden = !typedPath;
    jumpPath.textContent = typedPath;
    const query = typedPath ? '' : raw.toLowerCase();
    let visible = 0;
    for (const row of rows) {
      row.el.hidden = !!query && !row.dir.name.toLowerCase().includes(query);
      if (!row.el.hidden) visible += 1;
    }
    if (!data) return;
    if (!data.dirs.length) {
      setStatus(t('pm.noSubdirs') + (data.filesCount ? ` · ${t('pm.filesCount', { n: data.filesCount })}` : ''));
    } else if (!visible) {
      setStatus(t('pm.browseNoMatches', { q: raw }));
    } else setStatus('');
    note.textContent = t('pm.showingFirst', { n: data.dirs.length });
    note.hidden = !(data.truncated && !query);
    setRoving(visibleRows().find(row => row.dir.path === rovingPath) ?? visibleRows()[0] ?? null);
  }

  function setFilter(value: string): void {
    filter = value.slice(0, FILTER_MAX_CODE_UNITS);
    if (filterInput.value !== value) filterInput.value = value;
    clearFilter.hidden = !value;
    applyFilter();
  }

  function focusedRowTarget(): { path: string; action: string } | null {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !rowsEl.contains(active)) return null;
    const row = active.closest<HTMLElement>('.folder-browser-row');
    return row?.dataset.dirPath ? { path: row.dataset.dirPath, action: active.dataset.action ?? 'open' } : null;
  }

  function apply(next: ProjectBrowseData, focusList: boolean): void {
    const focused = focusedRowTarget();
    // A refresh of the same listing keeps an open delete confirmation; it only
    // takes focus back if the reader was inside it.
    const keepConfirm = confirming && data?.path === next.path ? confirming.row.dir.path : '';
    const confirmFocused = !!confirming && confirming.panel.contains(document.activeElement);
    closeConfirm();
    if (!create.hidden && next.path !== createParent) closeCreate();
    data = next;
    renderCrumbs(next.path);
    up.disabled = !next.parent;
    newFolder.disabled = false;
    rows = next.dirs.map(buildRow);
    rowsEl.replaceChildren(...rows.map(row => row.el));
    list.removeAttribute('aria-busy');
    applyFilter();
    renderFooter();
    const reconfirm = keepConfirm && rows.find(row => row.dir.path === keepConfirm && !row.el.hidden);
    if (reconfirm) { openConfirm(reconfirm, confirmFocused); return; }
    const keep = focused && rows.find(row => row.dir.path === focused.path && !row.el.hidden);
    if (keep) {
      setRoving(keep);
      const target = focused.action === 'add' ? keep.add : focused.action === 'delete' ? keep.remove : keep.open;
      (target ?? keep.open).focus();
    } else if (focusList || focused) focusRow(visibleRows()[0]);
  }

  async function navigate(path: string, focusList = false): Promise<void> {
    if (disposed) return;
    const target = String(path || '~');
    const refresh = !!data && data.path === target;
    requested = target;
    if (!refresh) {
      closeCreate();
      alert.hidden = true;
      if (filter) setFilter('');
    }
    const load = coordinator.load(target, showHidden, signal => options.browse(target, showHidden, signal));
    if (load.cached) apply(load.cached, focusList);
    else if (refresh) list.setAttribute('aria-busy', 'true');
    else {
      if (focusList || focusedRowTarget()) list.focus();
      rows = [];
      rowsEl.replaceChildren();
      jump.hidden = true;
      note.hidden = true;
      list.setAttribute('aria-busy', 'true');
      setStatus(t('common.loading'), true);
    }
    const outcome = await load.completion;
    if (disposed || outcome.kind === 'cancelled') return;
    if (outcome.kind === 'failed') {
      list.removeAttribute('aria-busy');
      setStatus('');
      if (load.cached && data?.path === target) {
        // The cached listing could not be confirmed (it may have been deleted
        // elsewhere): drop it rather than offer a stale folder.
        data = null;
        rows = [];
        rowsEl.replaceChildren();
        note.hidden = true;
        up.disabled = true;
        newFolder.disabled = true;
        renderFooter();
      }
      showAlert(outcome.message || t('folder.settings.browseFailed'), target);
      return;
    }
    apply(outcome.data, focusList && !load.cached);
  }

  /** Host cancellation (a closed modal) must leave the last listing, never a
   * spinner over an emptied list. */
  function cancelRead(): void {
    coordinator.cancel();
    list.removeAttribute('aria-busy');
    if (data && requested !== data.path) {
      requested = data.path;
      apply(data, false);
    } else if (!data) setStatus('');
  }

  function jumpToTypedPath(): void {
    const raw = filterInput.value.trim();
    if (!PATH_LIKE.test(raw)) return;
    // The jump control hides with the filter; keep keyboard focus in the pane.
    const fromJump = document.activeElement === jump;
    setFilter('');
    void navigate(raw, fromJump);
  }

  function openCreate(): void {
    if (!options.mkdir || !data) return;
    if (creating) { createInput.focus(); return; }
    closeConfirm();
    createParent = data.path;
    create.hidden = false;
    createError.hidden = true;
    createInput.value = '';
    createInput.setAttribute('aria-label', t('folder.createInHint', { dir: createParent }));
    list.scrollTop = 0;
    createInput.focus();
  }

  function closeCreate(restoreFocus = false): void {
    if (create.hidden) return;
    create.hidden = true;
    if (restoreFocus) newFolder.focus();
  }

  function submitCreate(): Promise<void> {
    if (!options.mkdir || !createParent || creating) return Promise.resolve();
    const name = createInput.value.trim();
    if (!name) { createInput.focus(); return Promise.resolve(); }
    const parent = createParent;
    return track(createFolder(parent, name)).then(created => {
      // Land inside the new folder unless the reader has already moved on
      // (a pending click-through counts); the parent re-reads on the way back.
      if (created && !disposed && requested === parent) return navigate(created, true);
      return undefined;
    });
  }

  /** Resolves to the created path, or '' when the server refused. */
  async function createFolder(parent: string, name: string): Promise<string> {
    if (!options.mkdir) return '';
    creating = true;
    createInput.readOnly = true;
    createSubmit.disabled = true;
    create.setAttribute('aria-busy', 'true');
    createError.hidden = true;
    try {
      const result = await commandResult(await options.mkdir(parent, name), t('folder.createFailed'));
      if (disposed) return '';
      options.notify?.(t('folder.created'), 'success');
      coordinator.invalidate(parent);
      closeCreate();
      return typeof result.path === 'string' && result.path ? result.path : joinPath(parent, name);
    } catch (error) {
      if (disposed) return '';
      const message = errorText(error, t('folder.createFailed'));
      if (create.hidden) {
        // The reader moved on; the closed panel cannot carry the refusal.
        showAlert(message);
        options.notify?.(message, 'error');
      } else {
        createError.textContent = message;
        createError.hidden = false;
        createInput.focus();
      }
      return '';
    } finally {
      creating = false;
      if (!disposed) {
        createInput.readOnly = false;
        createSubmit.disabled = false;
        create.removeAttribute('aria-busy');
      }
    }
  }

  function openConfirm(row: FolderRow, focusCancel = true): void {
    if (!options.rmdir) return;
    closeCreate();
    closeConfirm();
    const panel = node('div', 'folder-browser-confirm');
    panel.setAttribute('role', 'group');
    panel.setAttribute('aria-label', t('folder.deleteTitle'));
    const copy = node('p', 'folder-browser-confirm-text');
    copy.append(node('strong', '', t('folder.deleteDirConfirm', { name: row.dir.name })), node('span', '', t('folder.deleteDirHint')));
    const cancel = createButton({ label: t('common.cancel'), size: 'sm', variant: 'ghost', onClick: () => closeConfirm(true) });
    const remove = createButton({ label: t('common.delete'), size: 'sm', variant: 'danger', onClick: () => { void performDelete(row); } });
    const actions = node('div', 'folder-browser-confirm-actions');
    actions.append(cancel, remove);
    panel.append(copy, actions);
    panel.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      closeConfirm(true);
    });
    row.open.hidden = true;
    row.actions.hidden = true;
    row.el.dataset.confirming = 'true';
    row.el.append(panel);
    confirming = { row, panel };
    if (focusCancel) cancel.focus();
  }

  function closeConfirm(restoreFocus = false): void {
    if (!confirming) return;
    const { row, panel } = confirming;
    confirming = null;
    panel.remove();
    row.open.hidden = false;
    row.actions.hidden = false;
    delete row.el.dataset.confirming;
    if (restoreFocus) (row.remove ?? row.open).focus();
  }

  function performDelete(row: FolderRow): Promise<void> {
    if (!options.rmdir || !data) return Promise.resolve();
    const listing = data.path;
    return track(deleteFolder(row, listing)).then(() => {
      // Re-read server truth; this read is not part of the tracked command.
      if (!disposed && requested === listing) return navigate(listing);
      return undefined;
    });
  }

  async function deleteFolder(row: FolderRow, listing: string): Promise<void> {
    if (!options.rmdir || !data) return;
    const path = row.dir.path;
    const position = data.dirs.findIndex(dir => dir.path === path);
    const visible = visibleRows();
    const index = visible.indexOf(row);
    const neighbor = visible[index + 1] ?? visible[index - 1];
    closeConfirm();
    alert.hidden = true;
    // A pending read of this listing predates the delete; drop it so it cannot
    // repaint the row. The refresh below reads server truth instead.
    coordinator.cancel();
    // The deletion lands in a recoverable trash bin, so the row leaves in the
    // same task as the confirmation; server truth restores it on failure.
    data = { ...data, dirs: data.dirs.filter(dir => dir.path !== path) };
    rows = rows.filter(candidate => candidate !== row);
    row.el.remove();
    applyFilter();
    focusRow(neighbor);
    coordinator.invalidate(listing);
    coordinator.invalidate(path);
    const rollback = options.onDeleted?.(path);
    try {
      await commandResult(await options.rmdir(path), t('folder.deleteFailed'));
      if (disposed) return;
      options.notify?.(t('folder.deleted'), 'success');
    } catch (error) {
      if (disposed) return;
      if (typeof rollback === 'function') rollback();
      // Put the row back locally: the follow-up read may fail as well.
      if (data?.path === listing && position >= 0 && !data.dirs.some(dir => dir.path === path)) {
        const dirs = data.dirs.slice();
        dirs.splice(Math.min(position, dirs.length), 0, row.dir);
        apply({ ...data, dirs }, false);
      }
      const message = errorText(error, t('folder.deleteFailed'));
      showAlert(message);
      alert.scrollIntoView?.({ block: 'nearest' });
      options.notify?.(message, 'error');
    }
  }

  function addFolder(path: string): boolean {
    if (options.isAdded(path)) return false;
    const refusal = options.onAdd(path);
    if (refusal) {
      showAlert(refusal);
      alertIsRefusal = true;
      alert.scrollIntoView?.({ block: 'nearest' });
      return false;
    }
    if (alertIsRefusal) alert.hidden = true;
    syncAdded();
    return true;
  }

  function syncAdded(): void {
    rows.forEach(paintAdded);
    renderFooter();
  }

  crumbs.addEventListener('scroll', markClippedCrumbs, { passive: true });
  filterInput.addEventListener('input', () => setFilter(filterInput.value));
  filterInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' && isComposing(event)) return;
    if (event.key === 'Enter') {
      // Never submit a host form; Enter opens a typed path or the first match.
      event.preventDefault();
      const raw = filterInput.value.trim();
      if (PATH_LIKE.test(raw)) jumpToTypedPath();
      else if (raw && visibleRows()[0]) void navigate(visibleRows()[0].dir.path);
    } else if (event.key === 'Escape' && filterInput.value) {
      event.preventDefault();
      event.stopPropagation();
      setFilter('');
    } else if (event.key === 'ArrowDown' && visibleRows()[0]) {
      event.preventDefault();
      focusRow(visibleRows()[0]);
    }
  });
  createInput.addEventListener('keydown', event => {
    if (event.key === 'Enter' && isComposing(event)) return;
    if (event.key === 'Enter') { event.preventDefault(); void submitCreate(); }
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeCreate(true); }
  });
  rowsEl.addEventListener('focusin', event => {
    const row = rows.find(candidate => candidate.el.contains(event.target as Node));
    if (row && row.dir.path !== rovingPath) setRoving(row);
  });
  rowsEl.addEventListener('keydown', event => {
    const row = rows.find(candidate => candidate.el.contains(event.target as Node));
    if (!row || row.el.dataset.confirming) return;
    const visible = visibleRows();
    const index = visible.indexOf(row);
    let target: FolderRow | undefined;
    if (event.key === 'ArrowDown') target = visible[index + 1];
    else if (event.key === 'ArrowUp') {
      if (index === 0) { event.preventDefault(); filterInput.focus(); return; }
      target = visible[index - 1];
    } else if (event.key === 'Home') target = visible[0];
    else if (event.key === 'End') target = visible[visible.length - 1];
    else if (event.key === 'Backspace' && data?.parent) {
      event.preventDefault();
      void navigate(data.parent, true);
      return;
    } else if (event.key === 'Delete' && row.remove) {
      event.preventDefault();
      openConfirm(row);
      return;
    } else return;
    event.preventDefault();
    if (target) focusRow(target);
  });

  return {
    el,
    navigate: path => navigate(path),
    refresh: () => {
      // A pending click-through is the folder the reader asked for.
      const path = requested || data?.path || '';
      if (!path) return Promise.resolve();
      coordinator.invalidate(path);
      return navigate(path);
    },
    currentPath: () => data?.path ?? '',
    syncAdded,
    cancel: cancelRead,
    relabel: () => {
      applyCopy();
      if (data) apply(data, false);
    },
    busy: () => commands.size > 0,
    settled: async () => {
      while (commands.size) await Promise.allSettled([...commands]);
    },
    dispose: () => {
      disposed = true;
      coordinator.cancel();
    },
  };
}
