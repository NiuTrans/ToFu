// @vitest-environment node
/** Catalog delivery races run the authored retained adapter with real typed
 * mutation policy; only transport/timers and presentation ports are controlled. */
import { describe, expect, it, vi } from 'vitest';
import { createFolderPresetMutations, folderPresetWorkspace } from './folder-presets';
const { readFileSync } = await vi.importActual<{
  readFileSync(path: URL, encoding: string): string;
}>('node:fs');
const { runInNewContext } = await vi.importActual<{
  runInNewContext(source: string, context: Record<string, unknown>): unknown;
}>('node:vm');
const source = readFileSync(new URL('../../runtime/sections/core/folders.js', import.meta.url), 'utf8');
const pushSource = readFileSync(new URL('../../runtime/sections/core/conversation_invalidation.js', import.meta.url), 'utf8');
const pushFunction = pushSource.slice(pushSource.indexOf('function _onFoldersChangedPush(rawFrame)'), pushSource.indexOf('runtimeScope._onFoldersChangedPush ='));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes; }); return { promise, resolve }; }
function harness(list: ReturnType<typeof vi.fn>) {
  const context: Record<string, any> = { createFolderPresetMutations, folderPresetWorkspace,
    _folders: [], _foldersLoaded: false, Api: { folders: { list, create: async () => ({ id: 'new', name: 'new', revision: 1 }) } },
    renderConversationList: vi.fn(), console, setTimeout: vi.fn(), clearTimeout: vi.fn(),
    sessionStorage: { getItem: () => null }, conversations: [],
    narrowFoldersChangedFrame: (frame: unknown) => frame, _frameIsOurs: () => true,
    _scheduleFoldersRefresh: vi.fn(),
  };
  runInNewContext(source + '\n' + pushFunction + '\n apiTest = { loadFolders, createFolder, getFolders, areFoldersLoaded, _onFoldersChangedPush };', context);
  return context;
}

describe('folder catalog freshness at retained boundary', () => {
  it('creation during first read retains all saved folders and loaded means complete catalog', async () => {
    const first = deferred<any[]>(); const second = deferred<any[]>();
    const list = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const h = harness(list); const api = h.apiTest;
    const loading = api.loadFolders(); await api.createFolder('new', '');
    expect(api.areFoldersLoaded()).toBe(false);
    expect(list).toHaveBeenCalledTimes(1);
    first.resolve([{ id: 'existing', name: 'existing', revision: 2 }]);
    await Promise.resolve(); await Promise.resolve();
    expect(list).toHaveBeenCalledTimes(2);
    expect(api.areFoldersLoaded()).toBe(false);
    second.resolve([{ id: 'existing', name: 'existing', revision: 2 }, { id: 'new', name: 'new', revision: 1 }]);
    await loading;
    expect(api.areFoldersLoaded()).toBe(true);
    expect(api.getFolders().map((row: any) => row.id)).toEqual(['existing', 'new']);
  });
  it('delete push invalidates the old flight even if debounce refresh joins it', async () => {
    const first = deferred<any[]>();
    const list = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValueOnce([]);
    const h = harness(list); const api = h.apiTest;
    h._folders = [{ id: 'gone', name: 'gone', revision: 1 }];
    const loading = api.loadFolders();
    api._onFoldersChangedPush({ userId: 1, deletedFolderId: 'gone' });
    expect(api.getFolders()).toEqual([]);
    const joined = api.loadFolders(); // debounce expires while stale GET pending
    expect(list).toHaveBeenCalledTimes(1);
    first.resolve([{ id: 'gone', name: 'gone', revision: 1 }]);
    await Promise.all([loading, joined]);
    expect(list).toHaveBeenCalledTimes(2);
    expect(api.getFolders()).toEqual([]);
  });
});
