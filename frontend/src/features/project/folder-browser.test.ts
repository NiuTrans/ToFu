// @vitest-environment jsdom
/** Behavior specification for the shared server-folder browser used by the
 * Project Co-Pilot pane and sidebar folder settings. Real coordinator, fake
 * transport; only Response-shaped command results are synthetic. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createProjectBrowseCoordinator } from '../../core/project-browse-coordinator';
import { createFolderBrowser, folderCrumbs } from './folder-browser';
import type { FolderBrowserHandle, FolderBrowserOptions } from './folder-browser';

const t = (key: string, values?: Record<string, string | number>) => `${key}${values ? JSON.stringify(values) : ''}`;
const flush = async () => { for (let index = 0; index < 10; index++) await Promise.resolve(); };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}
const listing = (path: string, names: string[], extra: Record<string, unknown> = {}) => ({
  path, parent: path === '/' ? null : path.replace(/\/[^/]+$/, '') || '/', filesCount: 0,
  dirs: names.map(name => ({ name, path: `${path === '/' ? '' : path}/${name}` })), ...extra,
});
const response = (ok: boolean, body: unknown) => ({ ok, json: async () => body });
function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
}

const handles: FolderBrowserHandle[] = [];
function mount(overrides: Partial<FolderBrowserOptions> = {}) {
  const added: string[] = [];
  const options: FolderBrowserOptions = {
    t,
    coordinator: createProjectBrowseCoordinator(() => memoryStorage()),
    browse: vi.fn(async (path: string) => listing(path, ['alpha', 'beta'])),
    isAdded: path => added.includes(path),
    onAdd: vi.fn(path => { added.push(path); }),
    ...overrides,
  };
  const handle = createFolderBrowser(options);
  document.body.append(handle.el);
  handles.push(handle);
  return { handle, options, added, el: handle.el };
}
const rowPaths = (el: Element) => [...el.querySelectorAll<HTMLElement>('.folder-browser-row:not([hidden])')].map(row => row.dataset.dirPath);
const row = (el: Element, path: string) => el.querySelector<HTMLElement>(`.folder-browser-row[data-dir-path="${path}"]`)!;
const filter = (el: Element) => el.querySelector<HTMLInputElement>('.folder-browser-filter')!;
function type(input: HTMLInputElement, value: string) {
  input.value = value;
  input.dispatchEvent(new Event('input', { bubbles: true }));
}
function key(target: Element, name: string) {
  const event = new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}
function button(el: Element, label: string): HTMLButtonElement {
  const found = [...el.querySelectorAll('button')].find(control => control.textContent === label || control.getAttribute('aria-label') === label);
  if (!found) throw new Error(`Missing button ${label}`);
  return found;
}

afterEach(() => { handles.splice(0).forEach(handle => handle.dispose()); document.body.replaceChildren(); vi.restoreAllMocks(); });

describe('navigation, cache and cancellation', () => {
  it('paints a listing, then a cached revisit before its refresh resolves', async () => {
    const refresh = deferred<unknown>();
    const browse = vi.fn().mockResolvedValueOnce(listing('/root', ['alpha'])).mockReturnValueOnce(refresh.promise);
    const { handle, el } = mount({ browse });
    const first = handle.navigate('/root');
    expect(el.querySelector('.folder-browser-state')?.textContent).toBe('common.loading');
    await first;
    expect(rowPaths(el)).toEqual(['/root/alpha']);
    expect(el.querySelector('[aria-current="location"]')?.textContent).toBe('root');
    expect(handle.currentPath()).toBe('/root');

    void handle.navigate('/root');
    expect(rowPaths(el)).toEqual(['/root/alpha']);
    expect(el.querySelector('.folder-browser-state')?.textContent).not.toBe('common.loading');
    refresh.resolve(listing('/root', ['alpha', 'fresh'])); await flush();
    expect(rowPaths(el)).toEqual(['/root/alpha', '/root/fresh']);
  });

  it('cancel aborts the read and a late result never repaints; newer navigation wins', async () => {
    const pending: Array<{ path: string; signal?: AbortSignal; resolve: (value: unknown) => void }> = [];
    const browse = vi.fn((path: string, _hidden: boolean, signal?: AbortSignal) => new Promise(resolve => { pending.push({ path, signal, resolve }); }));
    const { handle, el } = mount({ browse });
    void handle.navigate('/root');
    handle.cancel();
    expect(pending[0].signal?.aborted).toBe(true);
    pending[0].resolve(listing('/root', ['late'])); await flush();
    expect(rowPaths(el)).toEqual([]);

    void handle.navigate('/slow');
    void handle.navigate('/fast');
    expect(pending[1].signal?.aborted).toBe(true);
    pending[2].resolve(listing('/fast', ['winner'])); await flush();
    pending[1].resolve(listing('/slow', ['stale'])); await flush();
    expect(rowPaths(el)).toEqual(['/fast/winner']);
  });

  it('shows read failures with a retry instead of an empty folder', async () => {
    const browse = vi.fn().mockResolvedValueOnce({ error: 'Permission denied' }).mockResolvedValueOnce(listing('/locked', ['ok']));
    const { handle, el } = mount({ browse });
    await handle.navigate('/locked');
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('Permission denied');
    button(el, 'folder.settings.retry').click(); await flush();
    expect(rowPaths(el)).toEqual(['/locked/ok']);
    expect(el.querySelector<HTMLElement>('.folder-browser-alert')?.hidden).toBe(true);
  });

  it('navigates by rows, breadcrumbs, up and the hidden-folder toggle', async () => {
    const { handle, el, options } = mount();
    await handle.navigate('/root');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('.folder-browser-open')!.click(); await flush();
    expect(handle.currentPath()).toBe('/root/alpha');
    expect(document.activeElement).toBe(row(el, '/root/alpha/alpha').querySelector('.folder-browser-open'));
    button(el, 'root').click(); await flush();
    expect(handle.currentPath()).toBe('/root');
    button(el, 'pm.goUp').click(); await flush();
    expect(handle.currentPath()).toBe('/');
    const hidden = button(el, 'pm.showHidden');
    hidden.click(); await flush();
    expect(hidden.getAttribute('aria-pressed')).toBe('true');
    expect(options.browse).toHaveBeenLastCalledWith('/', true, expect.any(AbortSignal));
  });

  it('builds breadcrumb trails for POSIX, Windows and home-relative paths', () => {
    expect(folderCrumbs('/a/b')).toEqual([{ label: '/', path: '/' }, { label: 'a', path: '/a' }, { label: 'b', path: '/a/b' }]);
    expect(folderCrumbs('C:\\Users\\me')).toEqual([{ label: 'C:\\', path: 'C:\\' }, { label: 'Users', path: 'C:\\Users' }, { label: 'me', path: 'C:\\Users\\me' }]);
    expect(folderCrumbs('~/src')).toEqual([{ label: '~', path: '~' }, { label: 'src', path: '~/src' }]);
  });
});

describe('filter and typed paths', () => {
  it('filters names case-insensitively within a bounded query and reports no matches', async () => {
    const browse = vi.fn(async () => ({ ...listing('/root', ['Alpha', 'beta']), truncated: true }));
    const { handle, el } = mount({ browse });
    await handle.navigate('/root');
    expect(el.querySelector<HTMLElement>('.folder-browser-note')?.hidden).toBe(false);
    type(filter(el), '  ALPHA ');
    expect(rowPaths(el)).toEqual(['/root/Alpha']);
    expect(el.querySelector<HTMLElement>('.folder-browser-note')?.hidden).toBe(true);
    type(filter(el), 'absent');
    expect(rowPaths(el)).toEqual([]);
    expect(el.textContent).toContain('pm.browseNoMatches{"q":"absent"}');
    type(filter(el), 'x'.repeat(300));
    expect(el.textContent).toContain(`pm.browseNoMatches{"q":"${'x'.repeat(256)}"}`);
    key(filter(el), 'Escape');
    expect(filter(el).value).toBe('');
    expect(rowPaths(el)).toEqual(['/root/Alpha', '/root/beta']);
  });

  it('Enter opens the first match, or jumps to a typed path, without submitting a host form', async () => {
    const form = document.createElement('form');
    const submit = vi.fn(event => event.preventDefault());
    form.addEventListener('submit', submit);
    const { handle, el } = mount();
    form.append(el); document.body.append(form);
    await handle.navigate('/root');
    type(filter(el), 'bet');
    expect(key(filter(el), 'Enter').defaultPrevented).toBe(true); await flush();
    expect(handle.currentPath()).toBe('/root/beta');
    expect(filter(el).value).toBe('');
    type(filter(el), '/srv/data');
    const jump = el.querySelector<HTMLButtonElement>('.folder-browser-jump')!;
    expect(jump.hidden).toBe(false);
    expect(jump.textContent).toContain('/srv/data');
    expect(rowPaths(el)).toHaveLength(2);
    key(filter(el), 'Enter'); await flush();
    expect(handle.currentPath()).toBe('/srv/data');
    expect(submit).not.toHaveBeenCalled();
  });

  it('renders untrusted names as text and keeps keyboard focus on one roving row', async () => {
    const browse = vi.fn(async (path: string) => listing(path, ['<img src=x onerror=alert(1)>', 'second', 'third']));
    const { handle, el } = mount({ browse });
    await handle.navigate('/root');
    expect(el.querySelector('img')).toBeNull();
    const opens = [...el.querySelectorAll<HTMLButtonElement>('.folder-browser-open')];
    expect(opens.map(open => open.tabIndex)).toEqual([0, -1, -1]);
    key(filter(el), 'ArrowDown');
    expect(document.activeElement).toBe(opens[0]);
    key(opens[0], 'ArrowDown');
    expect(document.activeElement).toBe(opens[1]);
    expect(opens.map(open => open.tabIndex)).toEqual([-1, 0, -1]);
    key(opens[1], 'End');
    expect(document.activeElement).toBe(opens[2]);
    key(opens[2], 'Backspace'); await flush();
    expect(handle.currentPath()).toBe('/');
  });
});

describe('workspace intents', () => {
  it('adds rows and the open folder through the host port and repaints added markers', async () => {
    const { handle, el, options, added } = mount();
    await handle.navigate('/root');
    const add = row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="add"]')!;
    add.click();
    expect(options.onAdd).toHaveBeenCalledWith('/root/alpha');
    expect(row(el, '/root/alpha').dataset.added).toBe('true');
    expect(add.getAttribute('aria-label')).toBe('folder.settings.rootAdded{"path":"alpha"}');
    add.click();
    expect(options.onAdd).toHaveBeenCalledTimes(1);
    const footer = el.querySelector<HTMLButtonElement>('.folder-browser-footer button')!;
    footer.click();
    expect(added).toEqual(['/root/alpha', '/root']);
    expect(footer.disabled).toBe(true);
    added.splice(0);
    handle.syncAdded();
    expect(row(el, '/root/alpha').dataset.added).toBe('false');
    expect(footer.disabled).toBe(false);
  });
  it('shows a host refusal in place and clears it once an add succeeds', async () => {
    let full = true;
    const added: string[] = [];
    const onAdd = vi.fn((path: string) => {
      if (full) return 'folder.settings.pathLimit';
      added.push(path);
      return undefined;
    });
    const { handle, el } = mount({ onAdd, isAdded: path => added.includes(path) });
    await handle.navigate('/root');
    const add = row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="add"]')!;
    add.click();
    const alert = el.querySelector<HTMLElement>('.folder-browser-alert')!;
    expect(alert.hidden).toBe(false);
    expect(alert.getAttribute('role')).toBe('alert');
    expect(alert.textContent).toBe('folder.settings.pathLimit');
    expect(row(el, '/root/alpha').dataset.added).toBe('false');
    full = false;
    add.click();
    expect(alert.hidden).toBe(true);
    expect(row(el, '/root/alpha').dataset.added).toBe('true');
  });
});

describe('create and delete folders', () => {
  it('creates inline, invalidates the parent and lands in the new folder', async () => {
    const mkdir = vi.fn(async () => response(true, { ok: true, path: '/root/new' }));
    const browse = vi.fn(async (path: string) => listing(path, path === '/root' ? ['alpha'] : []));
    const { handle, el } = mount({ browse, mkdir });
    await handle.navigate('/root');
    button(el, 'pm.newFolderHere').click();
    const input = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    expect(document.activeElement).toBe(input);
    input.value = 'new';
    expect(key(input, 'Enter').defaultPrevented).toBe(true); await flush();
    expect(mkdir).toHaveBeenCalledWith('/root', 'new');
    expect(handle.currentPath()).toBe('/root/new');
    expect(el.querySelector<HTMLElement>('.folder-browser-create')?.hidden).toBe(true);
    await handle.navigate('/root');
    expect(browse.mock.calls.filter(([path]) => path === '/root')).toHaveLength(2);
  });

  it('keeps the server reason visible when creation is refused', async () => {
    const mkdir = vi.fn(async () => response(false, { type: 'urn:tofu:problem:bad_request', status: 400, detail: 'Folder already exists' }));
    const { handle, el } = mount({ mkdir });
    await handle.navigate('/root');
    button(el, 'pm.newFolderHere').click();
    const input = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    input.value = 'alpha';
    button(el, 'folder.create').click(); await flush();
    expect(el.querySelector('.folder-browser-create-error')?.textContent).toBe('Folder already exists');
    expect(handle.currentPath()).toBe('/root');
    key(input, 'Escape');
    expect(el.querySelector<HTMLElement>('.folder-browser-create')?.hidden).toBe(true);
  });

  it('confirms inline, removes the row before the server answers and refreshes on success', async () => {
    const deletion = deferred<unknown>();
    const rmdir = vi.fn(() => deletion.promise);
    const notify = vi.fn();
    let deleted = false;
    const browse = vi.fn(async (path: string) => listing(path, deleted ? ['beta'] : ['alpha', 'beta']));
    const onDeleted = vi.fn(() => undefined);
    const { handle, el } = mount({ browse, rmdir, notify, onDeleted });
    await handle.navigate('/root');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    expect(el.textContent).toContain('folder.deleteDirConfirm{"name":"alpha"}');
    expect(document.activeElement?.textContent).toBe('common.cancel');
    key(document.activeElement!, 'Escape');
    expect(row(el, '/root/alpha').dataset.confirming).toBeUndefined();
    expect(rmdir).not.toHaveBeenCalled();

    key(row(el, '/root/alpha').querySelector('.folder-browser-open')!, 'Delete');
    button(el, 'common.delete').click();
    expect(rmdir).toHaveBeenCalledWith('/root/alpha');
    expect(onDeleted).toHaveBeenCalledWith('/root/alpha');
    expect(rowPaths(el)).toEqual(['/root/beta']);
    expect(document.activeElement).toBe(row(el, '/root/beta').querySelector('.folder-browser-open'));
    deleted = true;
    deletion.resolve(response(true, { ok: true })); await flush();
    expect(notify).toHaveBeenCalledWith('folder.deleted', 'success');
    expect(rowPaths(el)).toEqual(['/root/beta']);
  });

  it('rolls host state back and restores the row from server truth when deletion fails', async () => {
    const rollback = vi.fn();
    const rmdir = vi.fn(async () => response(false, { detail: 'Refusing to delete an active workspace root' }));
    const { handle, el } = mount({ rmdir, onDeleted: () => rollback });
    await handle.navigate('/root');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    button(el, 'common.delete').click(); await flush();
    expect(rollback).toHaveBeenCalledTimes(1);
    expect(el.querySelector('.folder-browser-alert')?.textContent).toContain('Refusing to delete an active workspace root');
    expect(rowPaths(el)).toEqual(['/root/alpha', '/root/beta']);
  });

  it('does not pull the reader back when a creation settles after they moved on', async () => {
    const creation = deferred<unknown>();
    const mkdir = vi.fn(() => creation.promise);
    const { handle, el } = mount({ mkdir });
    await handle.navigate('/root');
    button(el, 'pm.newFolderHere').click();
    const input = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    input.value = 'later';
    key(input, 'Enter');
    button(el, 'pm.newFolderHere').click();
    key(input, 'Enter');
    expect(mkdir).toHaveBeenCalledTimes(1);
    await handle.navigate('/elsewhere');
    creation.resolve(response(true, { ok: true, path: '/root/later' })); await flush();
    expect(handle.currentPath()).toBe('/elsewhere');
  });

  it('keeps a deleted row gone when an older read of the listing settles late', async () => {
    const refresh = deferred<unknown>();
    const deletion = deferred<unknown>();
    let deleted = false;
    const browse = vi.fn()
      .mockResolvedValueOnce(listing('/root', ['alpha', 'beta']))
      .mockReturnValueOnce(refresh.promise)
      .mockImplementation(async (path: string) => listing(path, deleted ? ['beta'] : ['alpha', 'beta']));
    const { handle, el } = mount({ browse, rmdir: vi.fn(() => deletion.promise) });
    await handle.navigate('/root');
    void handle.navigate('/root');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    button(el, 'common.delete').click();
    refresh.resolve(listing('/root', ['alpha', 'beta'])); await flush();
    expect(rowPaths(el)).toEqual(['/root/beta']);
    deleted = true;
    deletion.resolve(response(true, { ok: true })); await flush();
    expect(rowPaths(el)).toEqual(['/root/beta']);
  });

  it('keeps a click-through when a creation settles while the next listing is still loading', async () => {
    const creation = deferred<unknown>();
    const elsewhere = deferred<unknown>();
    const browse = vi.fn(async (path: string) => listing(path, ['alpha']));
    const { handle, el } = mount({ browse, mkdir: vi.fn(() => creation.promise) });
    await handle.navigate('/root');
    button(el, 'pm.newFolderHere').click();
    const input = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    input.value = 'later';
    key(input, 'Enter');
    browse.mockImplementationOnce(() => elsewhere.promise as Promise<ReturnType<typeof listing>>);
    void handle.navigate('/elsewhere');
    creation.resolve(response(true, { ok: true, path: '/root/later' })); await flush();
    elsewhere.resolve(listing('/elsewhere', ['x'])); await flush();
    expect(handle.currentPath()).toBe('/elsewhere');
  });

  it('ignores Enter while an input method is composing', async () => {
    const mkdir = vi.fn(async () => response(true, { ok: true, path: '/root/x' }));
    const { handle, el } = mount({ mkdir });
    await handle.navigate('/root');
    type(filter(el), 'alp');
    filter(el).dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true }));
    await flush();
    expect(handle.currentPath()).toBe('/root');
    button(el, 'pm.newFolderHere').click();
    const input = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    input.value = '新建';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 229, bubbles: true, cancelable: true }));
    await flush();
    expect(mkdir).not.toHaveBeenCalled();
  });

  it('a refresh of the same listing keeps an open create draft and delete confirmation', async () => {
    const { handle, el } = mount({ mkdir: vi.fn(), rmdir: vi.fn() });
    await handle.navigate('/root');
    button(el, 'pm.newFolderHere').click();
    const input = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    input.value = 'draft';
    await handle.refresh();
    expect(el.querySelector<HTMLElement>('.folder-browser-create')?.hidden).toBe(false);
    expect(input.value).toBe('draft');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    await handle.refresh();
    expect(row(el, '/root/alpha').dataset.confirming).toBe('true');
    expect(document.activeElement?.textContent).toBe('common.cancel');
  });

  it('reports refusals to the host, exposes pending commands and relabels on a language switch', async () => {
    let language = 'en';
    const translate = (key: string, values?: Record<string, string | number>) => `${language}:${t(key, values)}`;
    const deletion = deferred<unknown>();
    const notify = vi.fn();
    const { handle, el } = mount({ t: translate, notify, rmdir: vi.fn(() => deletion.promise) });
    await handle.navigate('/root');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    button(el, 'en:common.delete').click();
    expect(handle.busy()).toBe(true);
    const settled = handle.settled();
    deletion.resolve(response(false, { detail: 'Refused' }));
    await settled;
    expect(handle.busy()).toBe(false);
    expect(notify).toHaveBeenCalledWith('Refused', 'error');
    language = 'zh';
    handle.relabel();
    expect(filter(el).placeholder).toBe('zh:pm.browseSearchPlaceholder');
    expect(row(el, '/root/alpha').querySelector('.folder-browser-open')?.getAttribute('title')).toBe('zh:pm.openTitle{"name":"alpha"}');
    expect(el.querySelector('.folder-browser-footer button')?.textContent).toBe('zh:pm.addThisFolder');
  });

  it('a host cancel mid-load restores the last listing instead of a spinner', async () => {
    const pending = deferred<unknown>();
    const browse = vi.fn(async (path: string) => listing(path, ['alpha']));
    const { handle, el } = mount({ browse });
    await handle.navigate('/root');
    browse.mockImplementationOnce(() => pending.promise as Promise<ReturnType<typeof listing>>);
    void handle.navigate('/root/alpha');
    expect(el.querySelector('.folder-browser-state')?.textContent).toBe('common.loading');
    handle.cancel();
    expect(rowPaths(el)).toEqual(['/root/alpha']);
    expect(el.querySelector('[aria-current="location"]')?.textContent).toBe('root');
    expect(el.querySelector('.folder-browser-list')?.hasAttribute('aria-busy')).toBe(false);
    pending.resolve(listing('/root/alpha', ['late'])); await flush();
    expect(rowPaths(el)).toEqual(['/root/alpha']);
  });

  it('refresh and the hidden toggle follow a pending click-through', async () => {
    const pending = deferred<unknown>();
    const browse = vi.fn(async (path: string) => listing(path, ['alpha']));
    const { handle, el, options } = mount({ browse });
    await handle.navigate('/root');
    browse.mockImplementationOnce(() => pending.promise as Promise<ReturnType<typeof listing>>);
    void handle.navigate('/root/alpha');
    void handle.refresh();
    expect(browse).toHaveBeenLastCalledWith('/root/alpha', false, expect.anything());
    button(el, 'pm.showHidden').click();
    expect(options.browse).toHaveBeenLastCalledWith('/root/alpha', true, expect.anything());
    await flush();
    expect(handle.currentPath()).toBe('/root/alpha');
  });

  it('drops a cached listing the server can no longer confirm and says why', async () => {
    let gone = false;
    const browse = vi.fn(async (path: string) => (gone && path === '/root/alpha'
      ? { error: 'Not a directory: /root/alpha' } : listing(path, ['alpha'])));
    const { handle, el } = mount({ browse });
    await handle.navigate('/root/alpha');
    await handle.navigate('/root');
    gone = true;
    await handle.navigate('/root/alpha');
    expect(rowPaths(el)).toEqual([]);
    expect(el.querySelector('.folder-browser-alert')?.textContent).toContain('Not a directory');
    expect(el.querySelector<HTMLButtonElement>('.folder-browser-footer button')?.disabled).toBe(true);
  });

  it('a deleted folder is never painted from cache again', async () => {
    let deleted = false;
    const browse = vi.fn(async (path: string) => listing(path, deleted && path === '/root' ? ['beta'] : ['alpha', 'beta']));
    const { handle, el } = mount({ browse, rmdir: vi.fn(async () => response(true, { ok: true })) });
    await handle.navigate('/root/alpha');
    await handle.navigate('/root');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    deleted = true;
    button(el, 'common.delete').click(); await flush();
    const reads = browse.mock.calls.length;
    void handle.navigate('/root/alpha');
    expect(el.querySelector('.folder-browser-state')?.textContent).toBe('common.loading');
    expect(browse.mock.calls.length).toBe(reads + 1);
  });

  it('a background refresh keeps focus where the reader is; the jump button hands focus to the list', async () => {
    const { handle, el } = mount({ rmdir: vi.fn() });
    await handle.navigate('/root');
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    filter(el).focus();
    await handle.refresh();
    expect(row(el, '/root/alpha').dataset.confirming).toBe('true');
    expect(document.activeElement).toBe(filter(el));
    type(filter(el), '/srv');
    const jump = el.querySelector<HTMLButtonElement>('.folder-browser-jump')!;
    jump.focus(); jump.click(); await flush();
    expect(handle.currentPath()).toBe('/srv');
    expect(el.contains(document.activeElement)).toBe(true);
  });

  it('creation targets the folder named when the panel opened and reports late refusals', async () => {
    const creation = deferred<unknown>();
    const notify = vi.fn();
    const { handle, el } = mount({ notify, mkdir: vi.fn(() => creation.promise) });
    await handle.navigate('/root');
    button(el, 'pm.newFolderHere').click();
    const input = el.querySelector<HTMLInputElement>('.folder-browser-create-input')!;
    expect(input.getAttribute('aria-label')).toBe('folder.createInHint{"dir":"/root"}');
    input.value = 'late';
    key(input, 'Enter');
    await handle.navigate('/elsewhere');
    expect(el.querySelector<HTMLElement>('.folder-browser-create')?.hidden).toBe(true);
    creation.resolve(response(false, { detail: 'Disk full' })); await flush();
    expect(el.querySelector('.folder-browser-alert')?.textContent).toContain('Disk full');
    expect(notify).toHaveBeenCalledWith('Disk full', 'error');
  });

  it('restores a refused row locally even when the follow-up read fails; settling skips that read', async () => {
    const deletion = deferred<unknown>();
    const reread = deferred<unknown>();
    const browse = vi.fn(async (path: string) => listing(path, ['alpha', 'beta']));
    const { handle, el } = mount({ browse, rmdir: vi.fn(() => deletion.promise) });
    await handle.navigate('/root');
    browse.mockImplementationOnce(() => reread.promise as Promise<ReturnType<typeof listing>>);
    row(el, '/root/alpha').querySelector<HTMLButtonElement>('[data-action="delete"]')!.click();
    button(el, 'common.delete').click();
    const settled = handle.settled();
    deletion.resolve(response(false, { detail: 'Refused' }));
    await settled;
    expect(handle.busy()).toBe(false);
    expect(rowPaths(el)).toEqual(['/root/alpha', '/root/beta']);
    reread.resolve({ error: 'Read failed' }); await flush();
    expect(rowPaths(el)).toEqual(['/root/alpha', '/root/beta']);
  });

  it('hides create and delete controls when the host grants neither command', async () => {
    const { handle, el } = mount();
    await handle.navigate('/root');
    expect(button(el, 'pm.newFolderHere').hidden).toBe(true);
    expect(el.querySelector('[data-action="delete"]')).toBeNull();
  });
});
