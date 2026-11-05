/** Sidebar folder settings. Owns transient editor/native-dialog lifecycle only.
 * Entry: openFolderSettings, openFolderDeleteConfirmation. Ports own durable
 * folder commands; the shared folder browser (features/project/folder-browser)
 * reads, creates and deletes server directories but never binds an active
 * project. Delete this retained-runtime seam when sidebar composition becomes typed. */
import { createButton, createDialog, createField, createIconButton, setFieldError } from '../../ui';
import { createProjectBrowseCoordinator } from '../../core/project-browse-coordinator';
import { createFolderBrowser } from '../../features/project/folder-browser';
import { folderIcon } from '../../features/project/folder-icons';
import type { FolderIconName } from '../../features/project/folder-icons';

export interface FolderSettingsRecord {
  id: string;
  name: string;
  color?: string;
  projectPath?: string;
  projectPaths?: string[];
  readOnlyPaths?: string[];
  revision?: number;
}
export interface FolderSettingsValues {
  name: string;
  color: string;
  projectPaths: string[];
  readOnlyPaths: string[];
  expectedRevision?: number;
}
export interface FolderSettingsPorts {
  t: (key: string, values?: Record<string, string | number>) => string;
  create: (values: FolderSettingsValues) => Promise<unknown>;
  update: (id: string, values: FolderSettingsValues) => Promise<unknown>;
  browse: (path: string, showHidden: boolean, signal?: AbortSignal) => Promise<unknown>;
  recent: () => Promise<unknown>;
  /** Raw `/project/mkdir` and `/project/rmdir` responses; omitted ports hide the controls. */
  mkdir?: (parent: string, name: string) => Promise<unknown>;
  rmdir?: (path: string) => Promise<unknown>;
  /** Session cache shared with the Co-Pilot browser for cache-first paint. */
  browseStorage?: () => Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> | null;

  reload?: (id: string) => Promise<FolderSettingsRecord>;

  onSaved?: (result: unknown) => void;
  remove?: (id: string) => Promise<unknown>;
}
export interface FolderSettingsHandle { el: HTMLDialogElement; dispose: () => void }

interface RecentProject { path: string; count: number; exists: boolean }

function node<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = ''): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  el.className = className;
  el.textContent = text;
  return el;
}
function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
function requireAcknowledgement(result: unknown, message: string): void {
  if (result == null || result === false) throw new Error(message);
}
function cleanPaths(raw: string[] = []): string[] {
  return [...new Set(raw.map(path => path.trim()).filter(Boolean))];
}
function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}
/** A path inside a start-truncating RTL box; the isolate keeps it in LTR order. */
function isolatedPath(tag: 'div' | 'span', className: string, path: string): HTMLElement {
  const el = node(tag, className);
  const text = node('bdi', '', path);
  text.dir = 'ltr';
  el.append(text);
  return el;
}
function restoreSidebarFocus(opener: HTMLElement | null): void {
  if (opener?.isConnected) { opener.focus(); return; }
  // Confirmed writes may re-render the rail before this dialog is removed.
  // Find the equivalent control by stable identity rather than focusing a
  // detached trigger. A deleted folder falls back to the new-project control.
  const id = opener?.getAttribute('data-folder-config-id');
  const replacement = id ? [...document.querySelectorAll<HTMLElement>('[data-folder-config-id]')]
    .find(control => control.getAttribute('data-folder-config-id') === id) : null;
  const add = document.querySelector<HTMLElement>('.folder-tab-add')
    ?? document.querySelector<HTMLElement>('.folder-quickadd');
  (replacement ?? add)?.focus();
}
let dialogSequence = 0;

export function openFolderSettings(ports: FolderSettingsPorts, initial?: FolderSettingsRecord): FolderSettingsHandle {
  const { t } = ports;
  const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const seq = ++dialogSequence;
  let disposed = false;
  let saving = false;

  let expectedRevision = initial?.revision ?? 0;
  let reloadDialog: ReturnType<typeof createDialog> | null = null;
  let paths = cleanPaths(initial?.projectPaths ?? (initial?.projectPath ? [initial.projectPath] : []));
  const readOnly = new Set(cleanPaths(initial?.readOnlyPaths).filter(path => paths.includes(path)));
  let deleteDialog: FolderSettingsHandle | null = null;

  const form = node('form', 'folder-settings');

  const content = node('div', 'folder-settings-content');
  form.append(content);
  const intro = node('p', 'folder-settings-hint', t('folder.settings.futureOnly'));
  intro.id = `folder-settings-hint-${seq}`;
  content.append(intro);
  const fields = node('fieldset', 'folder-settings-fields');
  content.append(fields);
  const nameField = createField({ label: t('folder.settings.name'), value: initial?.name ?? '', required: true });
  const nameInput = nameField.control as HTMLInputElement;
  nameInput.maxLength = 50;
  nameInput.autocomplete = 'off';
  nameInput.autofocus = true;
  nameInput.placeholder = t('folder.namePh');
  const palette = [
    ['', 'automatic'], ['#6e56cf', 'violet'], ['#3b82f6', 'blue'], ['#10b981', 'green'],
    ['#f59e0b', 'amber'], ['#ef4444', 'red'], ['#ec4899', 'pink'], ['#8b5cf6', 'purple'], ['#14b8a6', 'teal'],
  ];
  const color = initial?.color ?? '';
  const colorChoices = palette.map(([value, key]) => ({ value, label: t(`folder.settings.color.${key}`) }));
  if (!colorChoices.some(choice => choice.value === color)) colorChoices.push({ value: color, label: t('folder.settings.color.custom') });
  const colorField = createField({ label: t('folder.settings.color'), value: color, choices: colorChoices });
  const identity = node('div', 'folder-settings-identity');
  identity.append(nameField.el, colorField.el);
  fields.append(identity);

  // Workbench: the shared server-folder browser beside the roots and recents rail.
  const workbench = node('div', 'folder-settings-workbench');
  const rail = node('div', 'folder-settings-rail');
  const workspace = node('section', 'folder-settings-workspace');
  workspace.setAttribute('aria-labelledby', `folder-settings-roots-${seq}`);
  const workspaceHeading = node('div', 'folder-settings-section-heading');
  const workspaceTitle = node('h3', 'folder-settings-heading', t('folder.projectPathLabel'));
  workspaceTitle.id = `folder-settings-roots-${seq}`;
  const workspaceCount = node('span', 'folder-settings-count');
  workspaceHeading.append(workspaceTitle, workspaceCount);
  const pathField = createField({ label: t('folder.settings.path') });
  const pathInput = pathField.control as HTMLInputElement;
  pathInput.maxLength = 512;
  pathInput.autocomplete = 'off';
  pathInput.spellcheck = false;
  pathInput.placeholder = t('folder.projectPathPh');
  pathField.el.classList.add('folder-settings-path-field');
  // Keep the kit field's label/error wiring; only the input shares a row with Add.
  const pathRow = node('div', 'folder-settings-path-row');
  pathInput.replaceWith(pathRow);
  const addButton = createButton({ label: t('folder.settings.addRoot'), onClick: () => addManualPath() });
  pathRow.append(pathInput, addButton);
  const rootList = node('ol', 'folder-settings-roots');
  rootList.setAttribute('aria-labelledby', workspaceTitle.id);
  const rootStatus = node('p', 'folder-settings-status folder-settings-announcement');
  rootStatus.setAttribute('role', 'status');
  workspace.append(workspaceHeading, pathField.el, rootList, rootStatus);
  const recents = node('section', 'folder-settings-recents');
  const recentList = node('div', 'folder-settings-recent-list');
  rail.append(workspace, recents);
  fields.append(workbench);
  // Empty workspace is valid. Only an explicit empty Add reports a path error.
  pathInput.addEventListener('input', () => setFieldError(pathField, null));

  const browser = createFolderBrowser({
    t,
    coordinator: createProjectBrowseCoordinator(() => ports.browseStorage?.() ?? null),
    browse: (path, showHidden, signal) => ports.browse(path, showHidden, signal),
    mkdir: ports.mkdir,
    rmdir: ports.rmdir,
    isAdded: path => paths.includes(path),
    // A browsed folder is not the typed path: its refusal belongs in the browser.
    onAdd: path => {
      const refusal = rootRefusal(path);
      if (refusal) return refusal;
      stagePath(path);
      return undefined;
    },
    onDeleted: path => {
      const index = paths.indexOf(path);
      if (index < 0) return undefined;
      const wasReadOnly = readOnly.has(path);
      paths.splice(index, 1);
      readOnly.delete(path);
      renderRoots();
      return () => {
        if (paths.includes(path)) return;
        paths.splice(Math.min(index, paths.length), 0, path);
        if (wasReadOnly) readOnly.add(path);
        renderRoots();
      };
    },
    notify: message => { rootStatus.textContent = message; },
  });
  workbench.append(browser.el, rail);

  function rootButton(icon: FolderIconName, label: string, action: string, onClick: () => void, pressed?: boolean): HTMLButtonElement {
    const button = createIconButton({ icon: folderIcon(icon), label, size: 'sm', pressed, onClick });
    button.dataset.action = action;
    return button;
  }

  function renderRoots(focusPath?: string, action?: string): void {
    rootList.replaceChildren();
    workspaceCount.textContent = String(paths.length);
    recentList.querySelectorAll<HTMLButtonElement>('button[data-path]').forEach(button => {
      button.disabled = paths.includes(button.dataset.path ?? '') || button.dataset.missing === 'true';
    });
    if (!paths.length) rootList.append(node('li', 'folder-settings-empty', t('folder.settings.blankWorkspace')));
    paths.forEach((path, index) => {
      const primary = index === 0;
      const row = node('li', 'folder-settings-root');
      row.dataset.path = path;
      row.dataset.primary = String(primary);
      row.dataset.readOnly = String(readOnly.has(path));
      const star = rootButton('star', `${t('folder.settings.makePrimary')}: ${path}`, 'primary', () => {
        if (primary) return;
        paths.splice(index, 1); paths.unshift(path);
        renderRoots(path, 'primary');
        rootStatus.textContent = t('folder.settings.reordered', { path, n: 1 });
      }, primary);
      star.title = t(primary ? 'folder.settings.primary' : 'folder.settings.makePrimary');
      // The primary root changes by promoting another one; its own star cannot be released.
      if (primary) star.setAttribute('aria-disabled', 'true');
      const copy = node('div', 'folder-settings-root-copy');
      const title = node('div', 'folder-settings-root-title');
      title.append(node('span', 'folder-settings-root-name', basename(path)));
      if (primary) title.append(node('span', 'folder-settings-badge', t('folder.settings.primary')));
      if (readOnly.has(path)) title.append(node('span', 'folder-settings-badge folder-settings-badge--muted', t('folder.settings.readOnly')));
      copy.append(title, isolatedPath('div', 'folder-settings-root-path', path));
      copy.title = path;
      const controls = node('div', 'folder-settings-root-actions');
      const locked = readOnly.has(path);
      const lock = rootButton(locked ? 'lock' : 'pencil', `${t('folder.settings.readOnly')}: ${path}`, 'readonly', () => {
        if (readOnly.has(path)) readOnly.delete(path); else readOnly.add(path);
        renderRoots(path, 'readonly');
      }, locked);
      lock.title = t(locked ? 'pm.lockRo' : 'pm.lockRw');
      controls.append(lock);
      for (const [key, offset] of [['up', -1], ['down', 1]] as const) {
        const target = index + offset;
        const button = rootButton(key, `${t(`folder.settings.${key}`)}: ${path}`, key, () => {
          [paths[index], paths[target]] = [paths[target], paths[index]];
          renderRoots(path, key);
          rootStatus.textContent = t('folder.settings.reordered', { path, n: target + 1 });
        });
        button.disabled = target < 0 || target >= paths.length;
        controls.append(button);
      }
      controls.append(rootButton('close', `${t('folder.settings.removeRoot')}: ${path}`, 'remove', () => {
        paths.splice(index, 1); readOnly.delete(path);
        renderRoots(); pathInput.focus();
      }));
      row.append(star, copy, controls);
      rootList.append(row);
    });
    browser.syncAdded();
    if (focusPath) {
      const row = [...rootList.children].find(child => (child as HTMLElement).dataset.path === focusPath);
      const target = row?.querySelector<HTMLButtonElement>(`[data-action="${action}"]`);
      // Moving to an endpoint disables that arrow; keep focus on the same root.
      (target && !target.disabled ? target : row?.querySelector<HTMLButtonElement>('[data-action="primary"]'))?.focus();
    }
  }
  /** Why `path` cannot become a root, or null when it can. */
  function rootRefusal(path: string): string | null {
    if (!path) return t('folder.settings.pathRequired');
    if (path.length > 512 || paths.length >= 32) return t('folder.settings.pathLimit');
    if (paths.includes(path)) return t('folder.settings.duplicatePath');
    return null;
  }
  function stagePath(path: string): void {
    paths.push(path); renderRoots();
    rootStatus.textContent = t('folder.settings.rootAdded', { path });
  }
  function addPath(raw: string): boolean {
    const path = raw.trim();
    const refusal = rootRefusal(path);
    setFieldError(pathField, refusal);
    if (!refusal) stagePath(path);
    return !refusal;
  }
  function addManualPath(): void {
    if (addPath(pathInput.value)) pathInput.value = '';
    pathInput.focus();
  }
  pathInput.addEventListener('keydown', event => {
    // An IME commit also fires Enter; it confirms text, not the Add action.
    if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); addManualPath(); }
  });

  // Recent projects: searchable picks beside the browser. Failures stay
  // visible and retryable rather than reading as an empty account.
  recents.setAttribute('aria-labelledby', `folder-settings-recent-${seq}`);
  const recentHeading = node('div', 'folder-settings-section-heading');
  const recentTitle = node('h3', 'folder-settings-heading', t('folder.projectPathRecent'));
  recentTitle.id = `folder-settings-recent-${seq}`;
  const recentCount = node('span', 'folder-settings-count');
  recentHeading.append(recentTitle, recentCount);
  const recentSearch = node('div', 'folder-settings-recent-search');
  const recentFilter = node('input', 'folder-settings-recent-filter');
  recentFilter.type = 'search';
  recentFilter.placeholder = t('pm.recentSearchPlaceholder');
  recentFilter.setAttribute('aria-label', t('pm.recentSearchPlaceholder'));
  recentFilter.autocomplete = 'off';
  recentFilter.spellcheck = false;
  const searchIcon = folderIcon('search');
  searchIcon.classList.add('folder-settings-recent-search-icon');
  const clearRecentFilter = createIconButton({ icon: folderIcon('close'), label: t('pm.recentClearSearch'), size: 'sm',
    onClick: () => { recentFilter.value = ''; renderRecents(); recentFilter.focus(); } });
  clearRecentFilter.hidden = true;
  recentSearch.append(searchIcon, recentFilter, clearRecentFilter);
  const recentStatus = node('p', 'folder-settings-hint');
  recentStatus.setAttribute('role', 'status');
  const recentError = node('p', 'folder-settings-error');
  recentError.setAttribute('role', 'alert'); recentError.hidden = true;
  const retryRecent = createButton({ label: t('folder.settings.retry'), size: 'sm', onClick: () => { void loadRecents(); } });
  retryRecent.hidden = true;
  recents.append(recentHeading, recentSearch, recentStatus, recentError, retryRecent, recentList);
  let recentProjects: RecentProject[] | null = null;
  let recentLoading = false;
  recentFilter.addEventListener('input', () => renderRecents());
  recentFilter.addEventListener('keydown', event => {
    if (event.key === 'Enter' && (event.isComposing || event.keyCode === 229)) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      recentList.querySelector<HTMLButtonElement>('button[data-path]:not(:disabled)')?.click();
    } else if (event.key === 'Escape' && recentFilter.value) {
      event.preventDefault(); event.stopPropagation();
      recentFilter.value = ''; renderRecents();
    }
  });
  function renderRecents(): void {
    recentList.replaceChildren();
    if (!recentProjects) return;
    const query = recentFilter.value.trim().toLowerCase();
    clearRecentFilter.hidden = !query;
    const visible = query ? recentProjects.filter(item => item.path.toLowerCase().includes(query)) : recentProjects;
    recentCount.textContent = query ? `${visible.length}/${recentProjects.length}` : String(recentProjects.length);
    recentStatus.textContent = visible.length ? '' : t(query ? 'pm.recentNoMatch' : 'folder.projectPathNoRecent');
    for (const item of visible) {
      const name = basename(item.path);
      const row = node('div', 'folder-settings-recent-item');
      const entry = node('button', 'folder-settings-recent-entry');
      entry.type = 'button';
      entry.dataset.path = item.path;
      entry.title = item.path;
      entry.setAttribute('aria-label', item.exists ? item.path : `${item.path} — ${t('pm.recentMissing')}`);
      const copy = node('span', 'folder-settings-recent-copy');
      const title = node('span', 'folder-settings-recent-title');
      title.append(node('span', 'folder-settings-recent-name', name));
      if (!item.exists) {
        entry.dataset.missing = 'true';
        title.append(node('span', 'folder-settings-badge folder-settings-badge--warning', t('pm.recentMissing')));
      }
      copy.append(title, isolatedPath('span', 'folder-settings-recent-path', item.path));
      entry.append(copy);
      if (item.count > 1) entry.append(node('span', 'folder-settings-recent-count', `×${item.count}`));
      entry.disabled = paths.includes(item.path) || !item.exists;
      entry.addEventListener('click', () => {
        // The entry disables once added; keep keyboard focus in the editor.
        if (addPath(item.path)) { entry.disabled = true; pathInput.focus(); }
      });
      row.append(entry);
      if (item.exists) {
        // Named by full path: two recents can share a folder name.
        const locate = createIconButton({ icon: folderIcon('locate'), label: t('pm.openTitle', { name: item.path }), size: 'sm',
          onClick: () => { void browser.navigate(item.path); } });
        row.append(locate);
      }
      recentList.append(row);
    }
  }
  async function loadRecents(): Promise<void> {
    if (disposed || recentLoading) return;
    recentLoading = true; recentError.hidden = true; retryRecent.hidden = true;
    recentStatus.textContent = t('folder.settings.loading');
    try {
      const result = await ports.recent();
      if (disposed) return;
      const raw = Array.isArray(result) ? result : (result as { projects?: unknown } | null)?.projects;
      if (!Array.isArray(raw) || !raw.every(item => item && typeof item.path === 'string')) {
        throw new Error(t('folder.settings.recentFailed'));
      }
      recentProjects = (raw as Array<{ path: string; count?: unknown; exists?: unknown }>).map(item => ({
        path: item.path, count: Math.max(0, Number(item.count) || 0), exists: item.exists !== false,
      }));
      renderRecents();
    } catch (error) {
      if (disposed) return;
      recentStatus.textContent = '';
      recentError.textContent = errorMessage(error, t('folder.settings.recentFailed'));
      recentError.hidden = false; retryRecent.hidden = false;
    } finally { recentLoading = false; }
  }
  renderRoots();

  const saveError = node('p', 'folder-settings-error');
  saveError.setAttribute('role', 'alert'); saveError.tabIndex = -1; saveError.hidden = true;
  content.append(saveError);

  const reload = createButton({ label: t('folder.settings.reload'), onClick: () => { void reviewReload(); } });
  reload.hidden = true;
  content.append(reload);
  async function reviewReload(): Promise<void> {
    if (disposed || saving || !initial || !ports.reload) return;
    reload.disabled = true;
    try {
      const fresh = await ports.reload(initial.id);
      if (disposed) return;
      if (!fresh) throw new Error(t('folder.settings.reloadFailed'));
      const review = node('div', 'folder-settings');
      review.append(node('p', 'folder-settings-hint', t('folder.settings.reloadHint')),
        node('p', 'folder-settings-delete-name', fresh.name));

      const savedColorLabel = colorChoices.find(choice => choice.value === (fresh.color ?? ''))?.label ?? t('folder.settings.color.custom');
      review.append(node('p', 'folder-settings-hint', `${t('folder.settings.color')}: ${savedColorLabel}`));
      const savedPaths = cleanPaths(fresh.projectPaths ?? (fresh.projectPath ? [fresh.projectPath] : []));
      const list = node('ul', 'folder-settings-review-roots');
      for (const path of savedPaths) list.append(node('li', 'folder-settings-full-path',
        `${path}${fresh.readOnlyPaths?.includes(path) ? ` — ${t('folder.settings.readOnly')}` : ''}`));
      review.append(list);
      const confirmation = createDialog({ title: t('folder.settings.reload'), body: review,
        actions: [{ id: 'cancel', label: t('folder.cancel') }, { id: 'reload', label: t('folder.settings.useSaved'), variant: 'primary' }] });
      reloadDialog = confirmation;
      const titleId = `folder-reload-title-${++dialogSequence}`;
      confirmation.el.querySelector('h2')!.id = titleId;
      confirmation.el.setAttribute('aria-labelledby', titleId);
      document.body.append(confirmation.el);
      const completion = confirmation.open();
      confirmation.el.querySelector<HTMLButtonElement>('button')?.focus();
      const choice = await completion;
      confirmation.el.remove(); reloadDialog = null;
      if (disposed) return;
      if (choice === 'reload') {
        // Only explicit acceptance replaces entries; opening this review and
        // receiving a 409 never changes the draft or its revision baseline.
        nameInput.value = fresh.name;
        const select = colorField.control as HTMLSelectElement;
        if (![...select.options].some(option => option.value === (fresh.color ?? ''))) {
          const custom = node('option', '', t('folder.settings.color.custom'));
          custom.value = fresh.color ?? ''; select.append(custom);
        }
        select.value = fresh.color ?? '';
        paths = savedPaths; readOnly.clear();
        for (const path of cleanPaths(fresh.readOnlyPaths)) if (paths.includes(path)) readOnly.add(path);
        expectedRevision = fresh.revision ?? 0;
        pathInput.value = ''; renderRoots();
        setFieldError(nameField, null); setFieldError(pathField, null);
        saveError.hidden = true; reload.hidden = true; nameInput.focus();
      } else reload.focus();
    } catch (error) {
      if (!disposed) { saveError.textContent = errorMessage(error, t('folder.settings.reloadFailed')); saveError.hidden = false; }
    } finally { if (!disposed) reload.disabled = false; }
  }
  const footer = node('div', 'folder-settings-footer');
  const cancel = createButton({ label: t('folder.cancel'), size: 'lg', onClick: () => dispose() });
  const save = createButton({ label: t(initial ? 'folder.settings.save' : 'folder.create'), size: 'lg', variant: 'primary', type: 'submit' });
  if (initial && ports.remove) {
    const remove = createButton({ label: t('folder.deleteAction'), variant: 'danger', onClick: () => {
      deleteDialog?.dispose();
      deleteDialog = openFolderDeleteConfirmation(t, initial, async id => {
        const result = await ports.remove!(id);
        requireAcknowledgement(result, t('folder.deleteFailed'));
        if (!disposed) dispose();
        return result;
      });
    } });
    footer.append(remove);
  }
  footer.append(cancel, save); form.append(footer);
  const dialog = createDialog({ title: t(initial ? 'folder.settings.title' : 'folder.createTitle'), appearance: 'project', body: form, onClose: () => dispose() });
  const close = createIconButton({ icon: node('span', '', '×'), label: t('folder.cancel'), onClick: () => dispose() });
  dialog.el.querySelector('h2')!.parentElement!.append(close);
  dialog.el.setAttribute('aria-labelledby', `folder-settings-title-${seq}`);
  dialog.el.querySelector('h2')!.id = `folder-settings-title-${seq}`;
  dialog.el.setAttribute('aria-describedby', intro.id);
  function dispose(): void {
    if (disposed) return;
    disposed = true; browser.dispose(); deleteDialog?.dispose();
    reloadDialog?.close(); reloadDialog?.el.remove();
    dialog.close(); dialog.el.remove();
    restoreSidebarFocus(opener);
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (disposed || saving) return;
    const name = nameInput.value.trim();
    if (!name) { setFieldError(nameField, t('folder.settings.nameRequired')); nameInput.focus(); return; }
    // Don't silently discard a path typed but not yet added.
    if (pathInput.value.trim() && !addPath(pathInput.value)) { pathInput.focus(); return; }
    pathInput.value = '';
    setFieldError(nameField, null); saveError.hidden = true;

    setFieldError(pathField, null);
    saving = true; fields.disabled = true; save.disabled = true; reload.disabled = true;
    footer.querySelectorAll<HTMLButtonElement>('button').forEach(button => { if (button !== cancel) button.disabled = true; });
    form.setAttribute('aria-busy', 'true');
    save.textContent = t('folder.settings.saving');
    // A directory delete in flight may still restore a root it unstaged.
    if (browser.busy()) await browser.settled();
    if (disposed) return;
    const values: FolderSettingsValues = { name, color: (colorField.control as HTMLSelectElement).value,
      projectPaths: [...paths], readOnlyPaths: paths.filter(path => readOnly.has(path)),
      ...(initial ? { expectedRevision } : {}) };
    try {
      const result = initial ? await ports.update(initial.id, values) : await ports.create(values);
      requireAcknowledgement(result, t('folder.settings.saveFailed'));
      if (!disposed) { ports.onSaved?.(result); dispose(); }
    } catch (error) {
      if (disposed) return;
      saveError.textContent = errorMessage(error, t('folder.settings.saveFailed'));
      saveError.hidden = false; reload.hidden = !initial || !ports.reload; saveError.focus();
    } finally {
      saving = false;
      if (!disposed) {
        fields.disabled = false; reload.disabled = false; form.removeAttribute('aria-busy');
        footer.querySelectorAll<HTMLButtonElement>('button').forEach(button => { button.disabled = false; });
        save.textContent = t(initial ? 'folder.settings.save' : 'folder.create');
      }
    }
  });
  document.body.append(dialog.el);
  void dialog.open(); nameInput.focus();
  void browser.navigate(paths[0] ?? '~');
  void loadRecents();
  return { el: dialog.el, dispose };
}

/** Safe native confirmation; Cancel gets initial focus, server rejection stays
 * visible. The port alone preserves/unassigns conversations after deletion. */
export function openFolderDeleteConfirmation(
  t: FolderSettingsPorts['t'], folder: FolderSettingsRecord, remove: (id: string) => Promise<unknown>,
): FolderSettingsHandle {
  const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  let disposed = false;
  let pending = false;
  const body = node('div', 'folder-settings');
  body.append(node('p', 'folder-settings-delete-name', folder.name), node('p', 'folder-settings-hint', t('folder.deleteHint')));
  const error = node('p', 'folder-settings-error');
  error.setAttribute('role', 'alert'); error.hidden = true; body.append(error);
  const actions = node('div', 'folder-settings-actions');
  const cancel = createButton({ label: t('folder.cancel'), onClick: () => dispose() });
  cancel.autofocus = true;
  const confirm = createButton({ label: t('folder.deleteAction'), variant: 'danger', onClick: () => { void submit(); } });
  actions.append(cancel, confirm); body.append(actions);
  const dialog = createDialog({ title: t('folder.deleteTitle'), body, onClose: () => dispose() });
  const titleId = `folder-delete-title-${++dialogSequence}`;
  dialog.el.querySelector('h2')!.id = titleId; dialog.el.setAttribute('aria-labelledby', titleId);
  function dispose(): void {
    if (disposed) return;
    disposed = true; dialog.close(); dialog.el.remove();
    restoreSidebarFocus(opener);
  }
  async function submit(): Promise<void> {
    if (pending || disposed) return;
    pending = true; confirm.disabled = true; error.hidden = true;
    body.setAttribute('aria-busy', 'true');
    try {
      requireAcknowledgement(await remove(folder.id), t('folder.deleteFailed'));
      if (!disposed) dispose();
    } catch (cause) {
      if (!disposed) { error.textContent = errorMessage(cause, t('folder.deleteFailed')); error.hidden = false; }
    } finally {
      pending = false;
      if (!disposed) { confirm.disabled = false; body.removeAttribute('aria-busy'); }
    }
  }
  document.body.append(dialog.el); void dialog.open(); cancel.focus();
  return { el: dialog.el, dispose };
}
