// @vitest-environment jsdom
/** Behavior specifications for the typed folder editor; only native dialog
 * methods are shimmed because jsdom has no top-layer implementation. The
 * shared browser's own contract lives in features/project/folder-browser.test.ts. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { openFolderDeleteConfirmation, openFolderSettings } from './folder-settings';
import type { FolderSettingsHandle, FolderSettingsPorts, FolderSettingsRecord } from './folder-settings';
import { mountFolderConfigControls } from './folder-config-control';

const handles: FolderSettingsHandle[] = [];
const t = (key: string, values?: Record<string, string | number>) => `${key}${values ? JSON.stringify(values) : ''}`;
const flush = async () => { for (let index = 0; index < 10; index++) await Promise.resolve(); };
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function ports(overrides: Partial<FolderSettingsPorts> = {}): FolderSettingsPorts {
  return { t, create: vi.fn().mockResolvedValue({ id: 'created' }), update: vi.fn().mockResolvedValue({ id: 'project' }),
    browse: vi.fn().mockResolvedValue({ path: '/home', parent: '/', dirs: [{ name: 'project', path: '/home/project' }] }),
    recent: vi.fn().mockResolvedValue([{ path: '/recent/project' }]), ...overrides };
}
const folder: FolderSettingsRecord = { id: 'project', name: 'Project', color: '#3b82f6', projectPaths: ['/one/shared', '/two/shared'], readOnlyPaths: ['/two/shared'], revision: 4 };
function open(p: FolderSettingsPorts = ports(), initial?: FolderSettingsRecord) {
  const handle = openFolderSettings(p, initial); handles.push(handle); return handle;
}
function input(el: Element, label: string): HTMLInputElement {
  const field = [...el.querySelectorAll('.ui-field')].find(field => field.querySelector('label')?.textContent?.startsWith(label));
  if (!field) throw new Error(`Missing field ${label}`);
  return field.querySelector('input')!;
}
function button(el: Element, label: string): HTMLButtonElement {
  const found = [...el.querySelectorAll('button')].find(button => button.textContent === label || button.getAttribute('aria-label') === label);
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
}
function submit(el: Element) { el.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); }
function add(el: Element, path: string) {
  input(el, 'folder.settings.path').value = path;
  button(el, 'folder.settings.addRoot').click();
}
function rootPaths(el: Element) { return [...el.querySelectorAll<HTMLElement>('.folder-settings-root')].map(row => row.dataset.path); }
function row(el: Element, path: string) { return [...el.querySelectorAll<HTMLElement>('.folder-settings-root')].find(row => row.dataset.path === path)!; }
function action(el: Element, name: string) { return el.querySelector<HTMLButtonElement>(`[data-action="${name}"]`)!; }
function recentEntry(el: Element, path: string) { return el.querySelector<HTMLButtonElement>(`.folder-settings-recent-list button[data-path="${path}"]`)!; }
const response = (ok: boolean, body: unknown) => ({ ok, json: async () => body });

beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: function(this: HTMLDialogElement) { this.open = true; } });
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function(this: HTMLDialogElement, result = '') {
    if (!this.open) return;
    this.open = false; this.returnValue = result; this.dispatchEvent(new Event('close'));
  } });
});
afterEach(() => { handles.splice(0).forEach(handle => handle.dispose()); document.body.replaceChildren(); vi.restoreAllMocks(); });

describe('folder settings commands and root editing', () => {
  it('creates with only a name and accessible naming; opening only reads folders and recents', async () => {
    const p = ports(); const { el } = open(p);
    expect(el.open).toBe(true);
    expect(document.getElementById(el.getAttribute('aria-labelledby')!)?.textContent).toBe('folder.createTitle');
    expect(p.browse).toHaveBeenCalledTimes(1); expect(p.browse).toHaveBeenCalledWith('~', false, expect.any(AbortSignal));
    expect(p.recent).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(input(el, 'folder.settings.name'));
    input(el, 'folder.settings.name').value = '  Name only  ';
    submit(el); await flush();
    expect(p.create).toHaveBeenCalledWith({ name: 'Name only', color: '', projectPaths: [], readOnlyPaths: [] });
    expect(el.isConnected).toBe(false);
  });
  it('clears an empty-Add error while typing and still allows name-only creation', async () => {
    const p = ports(); const { el } = open(p);
    const path = input(el, 'folder.settings.path');
    expect(path.hasAttribute('aria-invalid')).toBe(false);
    button(el, 'folder.settings.addRoot').click();
    expect(path.getAttribute('aria-invalid')).toBe('true');
    path.value = '/draft'; path.dispatchEvent(new Event('input', { bubbles: true }));
    expect(path.hasAttribute('aria-invalid')).toBe(false);
    path.value = ''; input(el, 'folder.settings.name').value = 'Name only';
    submit(el); await flush();
    expect(p.create).toHaveBeenCalledWith(expect.objectContaining({ projectPaths: [] }));
  });
  it('close control cancels without writes and restores opener focus', () => {
    const opener = document.createElement('button'); document.body.append(opener); opener.focus();
    const p = ports(); const { el } = open(p);
    el.querySelector<HTMLButtonElement>('.ui-dialog__header button')!.click();
    expect(el.isConnected).toBe(false); expect(document.activeElement).toBe(opener);
    expect(p.create).not.toHaveBeenCalled();
  });
  it('requires a name but never requires roots', async () => {
    const p = ports(); const { el } = open(p); submit(el); await flush();
    expect(p.create).not.toHaveBeenCalled(); expect(el.isConnected).toBe(true);
    expect(input(el, 'folder.settings.name').getAttribute('aria-invalid')).toBe('true');
  });
  it('commits a typed-but-not-added path when saving and carries read-only roots', async () => {
    const p = ports(); const { el } = open(p, folder);
    input(el, 'folder.settings.path').value = ' /third/root ';
    submit(el); await flush();
    expect(p.update).toHaveBeenCalledWith('project', { name: 'Project', color: '#3b82f6', projectPaths: ['/one/shared', '/two/shared', '/third/root'], readOnlyPaths: ['/two/shared'], expectedRevision: 4 });
  });
  it('keeps duplicate manual input visible and does not submit it silently', async () => {
    const p = ports(); const { el } = open(p, folder);
    input(el, 'folder.settings.path').value = '/one/shared'; submit(el); await flush();
    expect(p.update).not.toHaveBeenCalled();
    expect(input(el, 'folder.settings.path').value).toBe('/one/shared');
    expect(input(el, 'folder.settings.path').getAttribute('aria-invalid')).toBe('true');
  });
  it('shows full ambiguous paths, reorders with native buttons, preserves focus and primary/read-only flags', async () => {
    const p = ports(); const { el } = open(p, folder);
    expect(rootPaths(el)).toEqual(['/one/shared', '/two/shared']);
    expect([...el.querySelectorAll('.folder-settings-root-path')].map(path => path.textContent)).toEqual(['/one/shared', '/two/shared']);
    const up = action(row(el, '/two/shared'), 'up');
    up.focus(); up.click();
    expect(rootPaths(el)).toEqual(['/two/shared', '/one/shared']);
    // Reaching the top disables Up; focus stays on the same root.
    expect(document.activeElement).toBe(action(row(el, '/two/shared'), 'primary'));
    expect(action(row(el, '/two/shared'), 'primary').getAttribute('aria-pressed')).toBe('true');
    expect(action(row(el, '/two/shared'), 'readonly').getAttribute('aria-pressed')).toBe('true');
    expect(el.textContent).toContain('folder.settings.reordered{"path":"/two/shared","n":1}');
    action(row(el, '/two/shared'), 'down').click();
    expect(rootPaths(el)).toEqual(['/one/shared', '/two/shared']);
    action(row(el, '/two/shared'), 'primary').click(); expect(rootPaths(el)[0]).toBe('/two/shared');
    expect(document.activeElement).toBe(action(row(el, '/two/shared'), 'primary'));
    action(row(el, '/two/shared'), 'remove').click();
    submit(el); await flush();
    expect(p.update).toHaveBeenCalledWith('project', expect.objectContaining({ projectPaths: ['/one/shared'], readOnlyPaths: [] }));
  });
  it('adds via Enter without submitting and makes per-root read-only changes explicit', async () => {
    const p = ports(); const { el } = open(p);
    input(el, 'folder.settings.name').value = 'New';
    const path = input(el, 'folder.settings.path'); path.value = '/manual';
    path.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    expect(p.create).not.toHaveBeenCalled(); expect(rootPaths(el)).toEqual(['/manual']);
    const lock = action(row(el, '/manual'), 'readonly');
    expect(lock.getAttribute('aria-pressed')).toBe('false');
    lock.click();
    expect(action(row(el, '/manual'), 'readonly').getAttribute('aria-pressed')).toBe('true');
    expect(document.activeElement).toBe(action(row(el, '/manual'), 'readonly'));
    submit(el); await flush(); expect(p.create).toHaveBeenCalledWith(expect.objectContaining({ readOnlyPaths: ['/manual'] }));
  });
  it('awaits save, prevents duplicate submit, and retains edited entries on failure for retry', async () => {
    const write = deferred<unknown>(); const p = ports({ create: vi.fn().mockReturnValueOnce(write.promise).mockResolvedValueOnce({ id: 'ok' }) });
    const { el } = open(p); input(el, 'folder.settings.name').value = 'Kept'; add(el, '/kept');
    submit(el); submit(el);
    expect(p.create).toHaveBeenCalledTimes(1); expect(el.open).toBe(true);
    expect(el.querySelector('fieldset')?.disabled).toBe(true);
    write.reject(new Error('Disk unavailable')); await flush();
    expect(el.open).toBe(true); expect(input(el, 'folder.settings.name').value).toBe('Kept');
    expect(rootPaths(el)).toEqual(['/kept']); expect(el.textContent).toContain('Disk unavailable');
    expect(el.querySelector('fieldset')?.disabled).toBe(false);
    expect(button(el, 'folder.create').disabled).toBe(false);
    submit(el); await flush(); expect(p.create).toHaveBeenCalledTimes(2); expect(el.isConnected).toBe(false);
  });
  it('does not navigate or invoke saved callbacks after disposal during a successful write', async () => {
    const write = deferred<unknown>(); const onSaved = vi.fn();
    const p = ports({ create: vi.fn().mockReturnValue(write.promise), onSaved });
    const handle = open(p); input(handle.el, 'folder.settings.name').value = 'Saving'; submit(handle.el);
    handle.dispose(); write.resolve({ id: 'late-success' }); await flush();
    expect(onSaved).not.toHaveBeenCalled(); expect(document.querySelector('dialog')).toBeNull();
  });
  it('restores focus to the equivalent settings trigger after the rail is re-rendered', async () => {
    const rail = document.createElement('div'); document.body.append(rail);
    rail.innerHTML = '<button data-folder-config-id="project">Settings</button>';
    rail.querySelector<HTMLButtonElement>('button')!.focus();
    const p = ports({ onSaved: () => { rail.innerHTML = '<button data-folder-config-id="project">Settings refreshed</button>'; } });
    const { el } = open(p, folder); submit(el); await flush();
    expect(document.activeElement).toBe(rail.querySelector('button'));
  });
  it('enforces bounded roots without truncating entries and captures the legacy revision baseline', async () => {
    const p = ports(); const initial = { ...folder, projectPaths: Array.from({ length: 32 }, (_, index) => `/root/${index}`), revision: undefined };
    const { el } = open(p, initial); add(el, '/one-too-many');
    expect(rootPaths(el)).toHaveLength(32); expect(el.textContent).toContain('folder.settings.pathLimit');
    input(el, 'folder.settings.path').value = '';
    submit(el); await flush(); expect(p.update).toHaveBeenCalledWith('project', expect.objectContaining({ expectedRevision: 0 }));
  });
  it('treats a null write response as a visible failure, not a successful dismissal', async () => {
    const p = ports({ update: vi.fn().mockResolvedValue(null) }); const { el } = open(p, folder);
    submit(el); await flush(); expect(el.isConnected).toBe(true); expect(el.textContent).toContain('folder.settings.saveFailed');
  });
  it('reviews a conflict without overwriting draft until explicit acceptance and uses refreshed revision', async () => {
    const fresh = { ...folder, name: 'External edit', projectPaths: ['/saved'], readOnlyPaths: ['/saved'], revision: 8 };
    const p = ports({ update: vi.fn().mockRejectedValueOnce(new Error('409 conflict')).mockResolvedValueOnce(fresh), reload: vi.fn().mockResolvedValue(fresh) });
    const { el } = open(p, folder); input(el, 'folder.settings.name').value = 'Local draft'; add(el, '/draft');
    submit(el); await flush();
    button(el, 'folder.settings.reload').click(); await flush();
    let review = [...document.querySelectorAll('dialog')].find(dialog => dialog !== el)!;
    expect(review.textContent).toContain('/saved'); expect(review.textContent).toContain('folder.settings.readOnly');
    expect(input(el, 'folder.settings.name').value).toBe('Local draft');
    button(review, 'folder.cancel').click(); await flush();
    expect(rootPaths(el)).toContain('/draft');
    button(el, 'folder.settings.reload').click(); await flush();
    review = [...document.querySelectorAll('dialog')].find(dialog => dialog !== el)!;
    button(review, 'folder.settings.useSaved').click(); await flush();
    expect(input(el, 'folder.settings.name').value).toBe('External edit'); expect(rootPaths(el)).toEqual(['/saved']);
    submit(el); await flush(); expect(p.update).toHaveBeenLastCalledWith('project', expect.objectContaining({ expectedRevision: 8, readOnlyPaths: ['/saved'] }));
  });
});

describe('shared folder browser and recent ports', () => {
  it('opens on the primary root and stages browsed folders without writing until Save', async () => {
    const p = ports(); const { el } = open(p, folder); await flush();
    expect(p.browse).toHaveBeenCalledWith('/one/shared', false, expect.any(AbortSignal));
    const browser = el.querySelector('.folder-browser')!;
    browser.querySelector<HTMLButtonElement>('.folder-browser-row[data-dir-path="/home/project"] [data-action="add"]')!.click();
    expect(rootPaths(el)).toEqual(['/one/shared', '/two/shared', '/home/project']);
    expect(browser.querySelector<HTMLElement>('.folder-browser-row[data-dir-path="/home/project"]')?.dataset.added).toBe('true');
    action(row(el, '/home/project'), 'remove').click();
    expect(browser.querySelector<HTMLElement>('.folder-browser-row[data-dir-path="/home/project"]')?.dataset.added).toBe('false');
    expect(p.create).not.toHaveBeenCalled(); expect(p.update).not.toHaveBeenCalled();
  });
  it('explains a browsed root refused at the limit inside the browser, not on the manual path field', async () => {
    const initial = { ...folder, projectPaths: Array.from({ length: 32 }, (_, index) => `/root/${index}`) };
    const { el } = open(ports(), initial); await flush();
    el.querySelector<HTMLButtonElement>('.folder-browser-row[data-dir-path="/home/project"] [data-action="add"]')!.click();
    expect(rootPaths(el)).toHaveLength(32);
    expect(el.querySelector('.folder-browser-alert')?.textContent).toBe('folder.settings.pathLimit');
    expect(input(el, 'folder.settings.path').hasAttribute('aria-invalid')).toBe(false);
  });
  it('browses into a directory and adds the open folder from the footer', async () => {
    const p = ports(); const { el } = open(p); await flush();
    el.querySelector<HTMLButtonElement>('.folder-browser-row[data-dir-path="/home/project"] .folder-browser-open')!.click(); await flush();
    expect(p.browse).toHaveBeenLastCalledWith('/home/project', false, expect.any(AbortSignal));
    button(el, 'pm.addThisFolder').click();
    expect(rootPaths(el)).toEqual(['/home']); // mock's acknowledged absolute directory
    expect(p.create).not.toHaveBeenCalled(); expect(p.update).not.toHaveBeenCalled();
  });
  it('shows browse failures rather than an empty list and supports hidden directory intent', async () => {
    const p = ports({ browse: vi.fn().mockResolvedValueOnce({ error: 'Permission denied' }).mockResolvedValue({ path: '/home', parent: '/', dirs: [] }) });
    const { el } = open(p); await flush();
    expect(el.querySelector('.folder-browser-alert')?.textContent).toContain('Permission denied');
    button(el, 'pm.showHidden').click(); await flush();
    expect(p.browse).toHaveBeenLastCalledWith('~', true, expect.any(AbortSignal));
  });
  it('creates and deletes server directories, keeping default roots consistent with the server', async () => {
    const rmdir = vi.fn().mockResolvedValueOnce(response(false, { detail: 'Refused by server' })).mockResolvedValueOnce(response(true, { ok: true }));
    const mkdir = vi.fn().mockResolvedValue(response(true, { ok: true, path: '/home/new' }));
    let removed = false;
    const browse = vi.fn(async (path: string) => ({ path, parent: '/', dirs: path === '/home' && !removed ? [{ name: 'project', path: '/home/project' }] : [] }));
    const p = ports({ browse, mkdir, rmdir }); const { el } = open(p, { ...folder, projectPaths: ['/home', '/home/project'], readOnlyPaths: ['/home/project'] });
    await flush();
    const deleteRow = () => {
      el.querySelector<HTMLButtonElement>('.folder-browser-row[data-dir-path="/home/project"] [data-action="delete"]')!.click();
      button(el, 'common.delete').click();
    };
    deleteRow();
    expect(rootPaths(el)).toEqual(['/home']);
    await flush();
    expect(rootPaths(el)).toEqual(['/home', '/home/project']);
    expect(action(row(el, '/home/project'), 'readonly').getAttribute('aria-pressed')).toBe('true');
    expect(el.querySelector('.folder-browser-alert')?.textContent).toContain('Refused by server');
    removed = true; deleteRow(); await flush();
    expect(rootPaths(el)).toEqual(['/home']);
    expect(el.textContent).toContain('folder.deleted');
    button(el, 'pm.newFolderHere').click();
    const name = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    name.value = 'new';
    name.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })); await flush();
    expect(mkdir).toHaveBeenCalledWith('/home', 'new');
    expect(p.update).not.toHaveBeenCalled();
    expect(el.querySelector('[aria-current="location"]')?.textContent).toBe('new');
  });
  it('saving while a directory delete is pending waits for the server and keeps a refused root', async () => {
    const deletion = deferred<unknown>();
    const rmdir = vi.fn(() => deletion.promise);
    const browse = vi.fn(async (path: string) => ({ path, parent: '/', dirs: path === '/home' ? [{ name: 'project', path: '/home/project' }] : [] }));
    const p = ports({ browse, rmdir }); const { el } = open(p, { ...folder, projectPaths: ['/home', '/home/project'], readOnlyPaths: [] });
    await flush();
    el.querySelector<HTMLButtonElement>('.folder-browser-row[data-dir-path="/home/project"] [data-action="delete"]')!.click();
    button(el, 'common.delete').click();
    expect(rootPaths(el)).toEqual(['/home']);
    submit(el); await flush();
    expect(p.update).not.toHaveBeenCalled();
    deletion.resolve(response(false, { detail: 'Refused by server' })); await flush();
    expect(p.update).toHaveBeenCalledWith('project', expect.objectContaining({ projectPaths: ['/home', '/home/project'] }));
  });
  it('does not add a typed path on an input-method Enter', () => {
    const { el } = open(ports());
    const path = input(el, 'folder.settings.path'); path.value = '/ime';
    path.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true }));
    expect(rootPaths(el)).toEqual([]);
  });
  it('renders recent names separately with exact accessible paths and no HTML interpretation', async () => {
    const path = '/workspace/<img src=x onerror=alert(1)>/project';
    const p = ports({ recent: vi.fn().mockResolvedValue([{ path, count: 3 }]) });
    const { el } = open(p); await flush();
    const entry = recentEntry(el, path);
    expect(entry.querySelector('.folder-settings-recent-name')?.textContent).toBe('project');
    expect(entry.querySelector('.folder-settings-recent-path')?.textContent).toBe(path);
    expect(entry.getAttribute('aria-label')).toBe(path); expect(entry.title).toBe(path);
    expect(entry.textContent).toContain('×3');
    expect(el.querySelector('.folder-settings-recents img')).toBeNull(); entry.click();
    expect(rootPaths(el)).toEqual([path]); expect(entry.disabled).toBe(true);
    expect(document.activeElement).toBe(input(el, 'folder.settings.path'));
    expect(p.create).not.toHaveBeenCalled();
  });
  it('searches recents, adds the first match on Enter and locates a recent in the browser', async () => {
    const p = ports({ recent: vi.fn().mockResolvedValue({ projects: [{ path: '/srv/alpha' }, { path: '/srv/beta', exists: false }, { path: '/srv/gamma' }] }) });
    const { el } = open(p); await flush();
    expect(recentEntry(el, '/srv/beta').disabled).toBe(true);
    expect(recentEntry(el, '/srv/beta').textContent).toContain('pm.recentMissing');
    const search = el.querySelector<HTMLInputElement>('.folder-settings-recent-filter')!;
    search.value = 'GAM'; search.dispatchEvent(new Event('input', { bubbles: true }));
    expect([...el.querySelectorAll<HTMLButtonElement>('.folder-settings-recent-list button[data-path]')].map(entry => entry.dataset.path)).toEqual(['/srv/gamma']);
    expect(el.querySelector('.folder-settings-recents .folder-settings-count')?.textContent).toBe('1/3');
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
    search.dispatchEvent(enter);
    expect(enter.defaultPrevented).toBe(true);
    expect(rootPaths(el)).toEqual(['/srv/gamma']); expect(p.create).not.toHaveBeenCalled();
    search.value = 'nothing'; search.dispatchEvent(new Event('input', { bubbles: true }));
    expect(el.textContent).toContain('pm.recentNoMatch');
    search.value = ''; search.dispatchEvent(new Event('input', { bubbles: true }));
    button(el, 'pm.openTitle{"name":"/srv/alpha"}').click(); await flush();
    expect(p.browse).toHaveBeenLastCalledWith('/srv/alpha', false, expect.any(AbortSignal));
  });
  it('exposes null recent failures and retries successfully', async () => {
    const p = ports({ recent: vi.fn().mockResolvedValueOnce(null).mockResolvedValueOnce({ projects: [{ path: '/recent' }] }) });
    const { el } = open(p); await flush();
    expect(el.textContent).toContain('folder.settings.recentFailed');
    button(el, 'folder.settings.retry').click(); await flush(); button(el, '/recent').click();
    expect(rootPaths(el)).toEqual(['/recent']); expect(p.create).not.toHaveBeenCalled();

    action(row(el, '/recent'), 'remove').click();
    expect(button(el, '/recent').disabled).toBe(false);
    button(el, '/recent').click(); expect(rootPaths(el)).toEqual(['/recent']);
  });
  it('aborts browse and ignores late browse/recent/save results after native Escape-equivalent close', async () => {
    const browse = deferred<unknown>(); const recent = deferred<unknown>(); const save = deferred<unknown>();
    const p = ports({ browse: vi.fn().mockReturnValue(browse.promise), recent: vi.fn().mockReturnValue(recent.promise), create: vi.fn().mockReturnValue(save.promise) });
    const opener = document.createElement('button'); document.body.append(opener); opener.focus();
    const { el } = open(p);
    input(el, 'folder.settings.name').value = 'Pending'; submit(el);
    el.close(); expect(document.activeElement).toBe(opener);
    expect(vi.mocked(p.browse).mock.calls[0][2]?.aborted).toBe(true);
    browse.resolve({ path: '/late', dirs: [{ name: 'late', path: '/late/late' }] }); recent.resolve([{ path: '/late' }]); save.reject(new Error('late failure'));
    await flush(); expect(document.querySelector('dialog')).toBeNull(); expect(el.textContent).not.toContain('/late');
  });
});

describe('delete scope and sidebar discoverability', () => {
  it('focuses Cancel, removes only the captured folder, awaits confirmation and retains server errors', async () => {
    const remove = vi.fn().mockRejectedValueOnce(new Error('Rejected')).mockResolvedValueOnce(true);
    const handle = openFolderDeleteConfirmation(t, folder, remove); handles.push(handle); const { el } = handle;
    expect(document.activeElement).toBe(button(el, 'folder.cancel'));
    expect(el.textContent).toContain('folder.deleteHint');
    button(el, 'folder.deleteAction').click(); button(el, 'folder.deleteAction').click(); await flush();
    expect(remove).toHaveBeenCalledTimes(1); expect(remove).toHaveBeenCalledWith('project');
    expect(el.isConnected).toBe(true); expect(el.textContent).toContain('Rejected');
    button(el, 'folder.deleteAction').click(); await flush(); expect(el.isConnected).toBe(false);
  });
  it('cancel in the editor delete confirmation preserves the draft and only confirmed deletion closes both dialogs', async () => {
    const remove = vi.fn().mockResolvedValue(true);
    const p = ports({ remove }); const { el } = open(p, folder);
    input(el, 'folder.settings.name').value = 'Unsaved draft';
    button(el, 'folder.deleteAction').click();
    let confirm = [...document.querySelectorAll('dialog')].find(dialog => dialog !== el)!;
    button(confirm, 'folder.cancel').click();
    expect(remove).not.toHaveBeenCalled(); expect(input(el, 'folder.settings.name').value).toBe('Unsaved draft');
    button(el, 'folder.deleteAction').click();
    confirm = [...document.querySelectorAll('dialog')].find(dialog => dialog !== el)!;
    button(confirm, 'folder.deleteAction').click(); await flush();
    expect(remove).toHaveBeenCalledWith('project'); expect(document.querySelector('dialog')).toBeNull();
  });
  it('cancel never issues deletion; settings affordance has a name, factory and dialog semantics', () => {
    const remove = vi.fn(); const handle = openFolderDeleteConfirmation(t, folder, remove); handles.push(handle);
    button(handle.el, 'folder.cancel').click(); expect(remove).not.toHaveBeenCalled();
    const rail = document.createElement('div');
    rail.innerHTML = '<div class="folder-tab-row"><button class="folder-tab" data-folder-id="p" title="Project\nFull path"><span class="folder-tab-name">Project</span></button></div><button class="folder-tab-add">New project</button>';
    mountFolderConfigControls(rail, 'Project settings'); mountFolderConfigControls(rail, 'Project settings');
    expect(rail.querySelectorAll('.folder-tab-settings')).toHaveLength(1);
    const settings = rail.querySelector('.folder-tab-settings')!;
    expect(settings.getAttribute('aria-label')).toBe('Project settings: Project');
    expect(settings.getAttribute('aria-haspopup')).toBe('dialog'); expect(settings.classList.contains('ui-icon-btn')).toBe(true);
    expect(rail.querySelector('.folder-tab-add')?.classList.contains('folder-tab')).toBe(true);
    expect(rail.querySelector('.folder-tab-add')?.getAttribute('aria-haspopup')).toBe('dialog');
  });
});
