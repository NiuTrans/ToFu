/** Sidebar folder settings. Owns transient editor/native-dialog lifecycle only.
 * Entry: openFolderSettings, openFolderDeleteConfirmation. Ports own durable
 * folder commands. The workbench mounts the Project Co-Pilot's shared folder
 * browser (features/project/folder-browser) and workspace rail
 * (features/project/workspace-rail); they read, create and delete server
 * directories and stage roots but never bind an active project. Delete this
 * retained-runtime seam when sidebar composition becomes typed. */
import { createButton, createDialog, createField, createIconButton, setFieldError } from '../../ui';
import { createProjectBrowseCoordinator } from '../../core/project-browse-coordinator';
import { createFolderBrowser } from '../../features/project/folder-browser';
import { folderIcon } from '../../features/project/folder-icons';
import { createWorkspaceRail } from '../../features/project/workspace-rail';

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
  /** Clears the account's recent projects; omitted hides the rail's clear-all control. */
  recentClear?: () => Promise<unknown>;
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
  let deleteDialog: FolderSettingsHandle | null = null;

  const form = node('form', 'folder-settings');

  const content = node('div', 'folder-settings-content');
  form.append(content);
  // The dialog header carries this note as its subtitle, as the Co-Pilot does.
  const intro = node('p', 'folder-settings-hint', t('folder.settings.futureOnly'));
  intro.id = `folder-settings-hint-${seq}`;
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

  // Workbench: the Co-Pilot's shared folder browser beside its workspace rail.
  const workbench = node('div', 'folder-settings-workbench');
  fields.append(workbench);
  const rail = createWorkspaceRail({
    t,
    roots: () => paths,
    isReadOnly: path => readOnly.has(path),
    add: path => {
      const refusal = rootRefusal(path);
      if (!refusal) stagePath(path);
      return refusal;
    },
    remove: index => {
      const [path] = paths.splice(index, 1);
      if (path !== undefined) readOnly.delete(path);
      renderRoots();
    },
    toggleReadOnly: index => {
      const path = paths[index];
      if (path === undefined) return;
      if (readOnly.has(path)) readOnly.delete(path); else readOnly.add(path);
      renderRoots();
    },
    move: (from, to) => {
      const [path] = paths.splice(from, 1);
      if (path === undefined) return;
      paths.splice(to, 0, path);
      renderRoots();
    },
    // As in the Co-Pilot: stage the recent root, then show it in the browser.
    pickRecent: path => {
      const refusal = paths.includes(path) ? null : rootRefusal(path);
      if (!refusal && !paths.includes(path)) stagePath(path);
      void browser.navigate(path);
      return refusal;
    },
    clearRecents: ports.recentClear ? () => { void clearRecents(); } : undefined,
    retryRecents: () => { void loadRecents(); },
  });

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
    notify: message => rail.announce(message),
  });
  workbench.append(browser.el, rail.el);

  function renderRoots(): void {
    rail.renderRoots();
    browser.syncAdded();
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
    rail.announce(t('folder.settings.rootAdded', { path }));
  }

  // Failures stay visible and retryable rather than reading as an empty account.
  let recentLoading = false;
  async function loadRecents(): Promise<void> {
    if (disposed || recentLoading) return;
    recentLoading = true;
    try {
      const result = await ports.recent();
      if (disposed) return;
      const raw = Array.isArray(result) ? result : (result as { projects?: unknown } | null)?.projects;
      if (!Array.isArray(raw) || !raw.every(item => item && typeof item.path === 'string')) {
        throw new Error(t('folder.settings.recentFailed'));
      }
      rail.setRecents((raw as Array<{ path: string; count?: unknown; exists?: unknown }>).map(item => ({
        path: item.path, count: Math.max(0, Number(item.count) || 0), exists: item.exists !== false,
      })));
    } catch (error) {
      if (!disposed) rail.setRecents({ error: errorMessage(error, t('folder.settings.recentFailed')) });
    } finally { recentLoading = false; }
  }
  async function clearRecents(): Promise<void> {
    // As in the Co-Pilot, the re-read shows whatever the server kept.
    await ports.recentClear?.().catch(() => undefined);
    await loadRecents();
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
        rail.clearPendingPath(); renderRoots();
        setFieldError(nameField, null);
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
  // The Co-Pilot's header: folder glyph, title and the scope note beneath it.
  const title = dialog.el.querySelector('h2')!;
  const header = title.parentElement!;
  title.id = `folder-settings-title-${seq}`;
  const heading = node('div', 'folder-settings-title');
  const headingIcon = folderIcon('folder');
  headingIcon.classList.add('folder-settings-title-icon');
  const headingText = node('div', 'folder-settings-title-text');
  title.replaceWith(heading);
  headingText.append(title, intro);
  heading.append(headingIcon, headingText);
  const close = createIconButton({ icon: node('span', '', '×'), label: t('folder.cancel'), onClick: () => dispose() });
  header.append(close);
  dialog.el.setAttribute('aria-labelledby', title.id);
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
    if (!rail.commitPendingPath()) return;
    setFieldError(nameField, null); saveError.hidden = true;

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
