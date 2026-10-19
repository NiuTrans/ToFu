/** Sidebar folder settings. Owns transient editor/native-dialog lifecycle only.
 * Entry: openFolderSettings, openFolderDeleteConfirmation. Ports own durable
 * folder commands; browse/recent are read-only and never bind an active project.
 * Delete this retained-runtime seam when sidebar composition becomes typed. */
import { createButton, createChoice, createDialog, createField, createIconButton, setFieldError } from '../../ui';

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
  browse: (path: string, showHidden: boolean, signal: AbortSignal) => Promise<unknown>;
  recent: () => Promise<unknown>;

  reload?: (id: string) => Promise<FolderSettingsRecord>;

  onSaved?: (result: unknown) => void;
  remove?: (id: string) => Promise<unknown>;
}
export interface FolderSettingsHandle { el: HTMLDialogElement; dispose: () => void }

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
  let browseEpoch = 0;
  let browseAbort: AbortController | null = null;
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

  const workbench = node('div', 'folder-settings-workbench');
  const workspace = node('section', 'folder-settings-workspace');
  const workspaceHeading = node('div', 'folder-settings-section-heading');
  const workspaceCount = node('span', 'folder-settings-count');
  workspaceHeading.append(node('h3', 'folder-settings-heading', t('folder.projectPathLabel')), workspaceCount);
  workspace.append(workspaceHeading, node('p', 'folder-settings-hint', t('folder.settings.workspaceHint')));
  workbench.append(workspace);
  fields.append(workbench);
  const rootList = node('ol', 'folder-settings-roots');
  rootList.setAttribute('aria-label', t('folder.projectPathLabel'));
  workspace.append(rootList);
  const rootStatus = node('p', 'folder-settings-status folder-settings-announcement');
  rootStatus.setAttribute('role', 'status');
  workspace.append(rootStatus);
  const pathField = createField({ label: t('folder.settings.path'), hint: t('folder.settings.pathHint') });
  const pathInput = pathField.control as HTMLInputElement;
  pathInput.maxLength = 512;
  pathInput.autocomplete = 'off';
  pathInput.spellcheck = false;
  pathInput.placeholder = t('folder.projectPathPh');
  pathField.el.classList.add('folder-settings-path-field');
  workspace.append(pathField.el);
  const addActions = node('div', 'folder-settings-actions');
  const addButton = createButton({ label: t('folder.settings.addRoot'), onClick: () => addManualPath() });
  const browseButton = createButton({ label: t('folder.projectPathBrowse'), onClick: () => {
    browser.hidden = false;
    browseButton.setAttribute('aria-expanded', 'true');
    void loadDirectory(locationInput.value || paths[0] || '~');
    locationInput.focus();
  } });
  browseButton.setAttribute('aria-expanded', 'false');
  browseButton.setAttribute('aria-controls', `folder-settings-browser-${seq}`);
  addActions.append(addButton, browseButton);
  workspace.append(addActions);
  // Empty workspace is valid. Only an explicit empty Add reports a path error.
  pathInput.addEventListener('input', () => setFieldError(pathField, null));

  function renderRoots(focusPath?: string, action?: string): void {
    rootList.replaceChildren();

    workspaceCount.textContent = String(paths.length);

    fields.querySelectorAll<HTMLButtonElement>('.folder-settings-recent-list button').forEach(button => {
      button.disabled = paths.includes(button.dataset.path ?? '');
    });
    if (!paths.length) rootList.append(node('li', 'folder-settings-empty', t('folder.settings.blankWorkspace')));
    paths.forEach((path, index) => {
      const row = node('li', 'folder-settings-root');
      row.dataset.path = path;

      row.dataset.primary = String(index === 0);
      row.append(node('div', 'folder-settings-root-name', path.split(/[\\/]/).filter(Boolean).pop() ?? path));
      row.append(node('div', 'folder-settings-full-path', path));
      const controls = node('div', 'folder-settings-root-actions');
      const primary = createChoice({ type: 'radio', name: `folder-primary-${seq}`,
        label: t(index === 0 ? 'folder.settings.primary' : 'folder.settings.makePrimary'), checked: index === 0,
        onChange: () => { paths.splice(index, 1); paths.unshift(path); renderRoots(path, 'primary'); } });
      primary.input.dataset.action = 'primary';
      primary.input.setAttribute('aria-label', `${t('folder.settings.makePrimary')}: ${path}`);
      const readonly = createChoice({ label: t('folder.settings.readOnly'), checked: readOnly.has(path),
        onChange: checked => { if (checked) readOnly.add(path); else readOnly.delete(path); } });
      readonly.input.setAttribute('aria-label', `${t('folder.settings.readOnly')}: ${path}`);
      controls.append(primary.el, readonly.el);
      for (const [key, offset] of [['up', -1], ['down', 1]] as const) {
        const target = index + offset;
        const button = createButton({ label: t(`folder.settings.${key}`), size: 'sm', disabled: target < 0 || target >= paths.length,
          title: `${t(`folder.settings.${key}`)}: ${path}`, onClick: () => {
            [paths[index], paths[target]] = [paths[target], paths[index]];
            renderRoots(path, key);
            rootStatus.textContent = t('folder.settings.reordered', { path, n: target + 1 });
          } });
        button.setAttribute('aria-label', `${t(`folder.settings.${key}`)}: ${path}`);
        button.dataset.action = key;
        controls.append(button);
      }
      const remove = createButton({ label: t('folder.settings.removeRoot'), size: 'sm', variant: 'ghost', onClick: () => {
        paths.splice(index, 1); readOnly.delete(path);
        renderRoots(); pathInput.focus();
      } });
      remove.setAttribute('aria-label', `${t('folder.settings.removeRoot')}: ${path}`);
      controls.append(remove);
      row.append(controls);
      rootList.append(row);
    });
    if (focusPath) {
      const row = [...rootList.children].find(child => (child as HTMLElement).dataset.path === focusPath);
      const target = row?.querySelector<HTMLInputElement | HTMLButtonElement>(`[data-action="${action}"]`);
      // Moving to an endpoint disables that arrow; keep focus on the same root.
      (target && !target.disabled ? target : row?.querySelector<HTMLInputElement>('input'))?.focus();
    }
  }
  function addPath(raw: string): boolean {
    const path = raw.trim();
    if (!path) { setFieldError(pathField, t('folder.settings.pathRequired')); return false; }
    if (path.length > 512 || paths.length >= 32) { setFieldError(pathField, t('folder.settings.pathLimit')); return false; }
    if (paths.includes(path)) { setFieldError(pathField, t('folder.settings.duplicatePath')); return false; }
    setFieldError(pathField, null);
    paths.push(path); renderRoots();
    rootStatus.textContent = t('folder.settings.rootAdded', { path });
    return true;
  }
  function addManualPath(): void {
    if (addPath(pathInput.value)) pathInput.value = '';
    pathInput.focus();
  }
  pathInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); addManualPath(); }
  });
  renderRoots();

  // Read-only server directory browser. Only successful, current responses
  // enable selection; stale/aborted responses cannot resurrect a closed editor.
  const browser = node('section', 'folder-settings-browser');
  browser.id = `folder-settings-browser-${seq}`;
  browser.hidden = true;
  browser.setAttribute('aria-label', t('folder.projectPathBrowse'));
  const locationField = createField({ label: t('folder.settings.browseLocation'), value: '~' });
  const locationInput = locationField.control as HTMLInputElement;
  locationInput.autocomplete = 'off';
  locationInput.spellcheck = false;
  const browserActions = node('div', 'folder-settings-actions');
  let currentDirectory = '';
  let parentDirectory: string | null = null;
  const go = createButton({ label: t('folder.settings.go'), onClick: () => { void loadDirectory(locationInput.value); } });
  const up = createButton({ label: t('folder.settings.parent'), disabled: true, onClick: () => { if (parentDirectory) void loadDirectory(parentDirectory); } });
  const home = createButton({ label: t('folder.settings.home'), onClick: () => { void loadDirectory('~'); } });
  const use = createButton({ label: t('folder.settings.useDirectory'), disabled: true, onClick: () => {
    if (addPath(currentDirectory)) {
      browser.hidden = true; browseButton.setAttribute('aria-expanded', 'false'); pathInput.focus();
    }
  } });
  const hideBrowser = createButton({ label: t('folder.settings.closeBrowser'), variant: 'ghost', onClick: () => {
    browseEpoch++; browseAbort?.abort(); browser.hidden = true;
    browseButton.setAttribute('aria-expanded', 'false'); browseButton.focus();
  } });
  browserActions.append(go, up, home, use, hideBrowser);
  const hiddenChoice = createChoice({ label: t('folder.settings.showHidden'), onChange: () => { void loadDirectory(locationInput.value); } });
  const browserStatus = node('p', 'folder-settings-hint');
  browserStatus.setAttribute('role', 'status');
  const browserError = node('p', 'folder-settings-error');
  browserError.setAttribute('role', 'alert');
  browserError.hidden = true;
  const directories = node('div', 'folder-settings-directories');
  browser.append(locationField.el, browserActions, hiddenChoice.el, browserStatus, browserError, directories);
  workspace.append(browser);
  locationInput.addEventListener('input', () => {
    // A home read may still be pending while the user types a location.
    // Its response must not replace that text before Go/Enter captures it.
    browseEpoch++; browseAbort?.abort();
    use.disabled = true; up.disabled = true;
    browserStatus.textContent = ''; directories.replaceChildren();
    browser.removeAttribute('aria-busy');
  });
  locationInput.addEventListener('keydown', event => {

    if (event.key === 'Enter') { event.preventDefault(); void loadDirectory(locationInput.value); }
  });
  async function loadDirectory(path: string): Promise<void> {
    const epoch = ++browseEpoch;
    browseAbort?.abort(); browseAbort = new AbortController();
    use.disabled = true; up.disabled = true;
    browserError.hidden = true;
    browserStatus.textContent = t('folder.settings.loading');
    directories.replaceChildren();
    browser.setAttribute('aria-busy', 'true');
    try {
      const result = await ports.browse(path.trim() || '~', hiddenChoice.input.checked, browseAbort.signal);
      if (disposed || epoch !== browseEpoch) return;
      const data = result as { path?: unknown; parent?: unknown; dirs?: unknown; error?: unknown; truncated?: boolean } | null;
      if (typeof data?.error === 'string') throw new Error(data.error);
      if (!data || typeof data.path !== 'string' || !Array.isArray(data.dirs)) throw new Error(t('folder.settings.browseFailed'));
      if (!data.dirs.every(dir => dir && typeof dir.path === 'string' && typeof dir.name === 'string')) throw new Error(t('folder.settings.browseFailed'));
      currentDirectory = data.path;
      locationInput.value = data.path;
      parentDirectory = typeof data.parent === 'string' ? data.parent : null;
      up.disabled = !parentDirectory;
      use.disabled = false;
      browserStatus.textContent = data.truncated ? t('folder.settings.browseTruncated') : (data.dirs.length ? '' : t('folder.settings.noDirectories'));
      for (const dir of data.dirs as Array<{ name: string; path: string }>) {
        const entry = createButton({ label: dir.name, wrap: true, variant: 'ghost', title: dir.path, onClick: () => { void loadDirectory(dir.path); } });

        const copy = node('span', 'folder-settings-directory-name', dir.name);
        entry.replaceChildren(copy);
        directories.append(entry);
      }
    } catch (error) {
      if (disposed || epoch !== browseEpoch) return;
      browserStatus.textContent = '';
      browserError.textContent = errorMessage(error, t('folder.settings.browseFailed'));
      browserError.hidden = false;
    } finally {
      if (!disposed && epoch === browseEpoch) browser.removeAttribute('aria-busy');
    }
  }

  // No startup recent-project request: disclose on intent, never mask errors
  // as an empty account. A failed read remains retryable inside the disclosure.
  const recents = node('details', 'folder-settings-recents');
  recents.append(node('summary', '', t('folder.projectPathRecent')));
  const recentContent = node('div', 'folder-settings-recent-list');
  const recentStatus = node('p', 'folder-settings-hint');
  recentStatus.setAttribute('role', 'status');
  const recentError = node('p', 'folder-settings-error');
  recentError.setAttribute('role', 'alert'); recentError.hidden = true;
  let recentLoading = false;
  let recentLoaded = false;
  const retryRecent = createButton({ label: t('folder.settings.retry'), onClick: () => { void loadRecents(); } });
  retryRecent.hidden = true;
  recents.append(recentStatus, recentError, retryRecent, recentContent);
  workbench.append(recents);
  recents.addEventListener('toggle', () => { if (recents.open && !recentLoaded) void loadRecents(); });
  async function loadRecents(): Promise<void> {
    if (disposed || recentLoading) return;
    recentLoading = true; recentError.hidden = true; retryRecent.hidden = true;
    recentStatus.textContent = t('folder.settings.loading');
    try {
      const result = await ports.recent();
      if (disposed) return;
      const raw = Array.isArray(result) ? result : (result as { projects?: unknown } | null)?.projects;
      if (!Array.isArray(raw)) throw new Error(t('folder.settings.recentFailed'));
      if (!raw.every(item => item && typeof item.path === 'string')) throw new Error(t('folder.settings.recentFailed'));
      recentLoaded = true;
      recentStatus.textContent = raw.length ? '' : t('folder.projectPathNoRecent');
      recentContent.replaceChildren();
      for (const item of raw as Array<{ path: string }>) {
        const entry = createButton({ label: item.path, wrap: true, variant: 'ghost', title: item.path, onClick: () => {
          if (addPath(item.path)) { entry.disabled = true; pathInput.focus(); }
        } });
        entry.disabled = paths.includes(item.path);

        entry.setAttribute('aria-label', item.path);
        const copy = node('span', 'folder-settings-recent-copy');
        const name = item.path.split(/[\\/]/).filter(Boolean).pop() ?? item.path;
        copy.append(node('span', 'folder-settings-recent-name', name),
          node('span', 'folder-settings-recent-path', item.path));
        entry.replaceChildren(copy);
        entry.dataset.path = item.path;
        recentContent.append(entry);
      }
    } catch (error) {
      if (disposed) return;
      recentStatus.textContent = '';
      recentError.textContent = errorMessage(error, t('folder.settings.recentFailed'));
      recentError.hidden = false; retryRecent.hidden = false;
    } finally { recentLoading = false; }
  }

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
      const list = node('ul', 'folder-settings-roots');
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
    disposed = true; browseEpoch++; browseAbort?.abort(); deleteDialog?.dispose();
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
