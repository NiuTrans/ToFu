import { describe, expect, it, vi } from 'vitest';
import { createFolderPresetMutations, folderPresetWorkspace, type FolderPreset } from './folder-presets';
const deferred = <T>() => { let resolve!: (value: T) => void; let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject }; };

describe('confirmed folder presets', () => {
  it('retains ordered readonly roots and legacy scalar without mutating records', () => {
    const row = { projectPaths: ['/code', '/ref', '/code'], readOnlyPaths: ['/ref', '/foreign'] };
    expect(folderPresetWorkspace(row)).toEqual({ projectPath: '/code', projectPaths: ['/code', '/ref'], readOnlyPaths: ['/ref'] });
    expect(folderPresetWorkspace({ projectPath: '/legacy' }).projectPaths).toEqual(['/legacy']);
    expect(row.projectPaths).toHaveLength(3);
  });
  it('failed saves expose neither optimistic roots nor whole-record rollback', async () => {
    let rows: FolderPreset[] = [{ id: 'a', name: 'old', revision: 2, projectPaths: ['/old'] }];
    const save = deferred<FolderPreset>();
    const owner = createFolderPresetMutations({ list: () => rows, replace: value => { rows = value; },
      create: vi.fn(), update: () => save.promise });
    const result = owner.update('a', { name: 'edited', projectPaths: ['/unsaved'] });
    expect(folderPresetWorkspace(rows[0]).projectPath).toBe('/old');
    rows[0] = { ...rows[0], name: 'remote newer', revision: 3 };
    save.reject(new Error('network failed'));
    await expect(result).rejects.toThrow('network failed');
    expect(rows[0].name).toBe('remote newer');
  });
  it('serializes pending mutations, releases failed flights and uses fresh revision', async () => {
    let rows: FolderPreset[] = [{ id: 'a', name: 'old', revision: 1 }];
    const first = deferred<FolderPreset>();
    const update = vi.fn().mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce({ id: 'a', name: 'second', revision: 2 })
      .mockResolvedValueOnce({ id: 'a', name: 'third', revision: 3 });
    const owner = createFolderPresetMutations({ list: () => rows, replace: value => { rows = value; }, create: vi.fn(), update });
    const failed = owner.update('a', { name: 'first' });
    const next = owner.update('a', { name: 'second' });
    await Promise.resolve(); await Promise.resolve();
    expect(update).toHaveBeenCalledTimes(1);
    first.reject(new Error('failed')); await expect(failed).rejects.toThrow('failed');
    await next;
    expect(update.mock.calls[1][1].expectedRevision).toBe(1);
    await owner.update('a', { name: 'third' });
    expect(update.mock.calls[2][1].expectedRevision).toBe(2);
    expect(rows[0].name).toBe('third');
  });
  it('a list read started before create ACK cannot erase the created folder', async () => {
    let rows: FolderPreset[] = [];
    const owner = createFolderPresetMutations({ list: () => rows, replace: value => { rows = value; },
      create: async () => ({ id: 'new', name: 'new', revision: 1 }), update: vi.fn() });
    const started = owner.revision();
    await owner.create('new', '');
    expect(owner.reconcile([], started)).toBe(false);
    expect(rows).toHaveLength(1);
    expect(owner.reconcile(rows, owner.revision())).toBe(true);
  });
  it('explicit stale editor revision is not silently rewritten', async () => {
    let rows: FolderPreset[] = [{ id: 'a', name: 'old', revision: 4 }];
    const update = vi.fn().mockRejectedValue(new Error('conflict'));
    const owner = createFolderPresetMutations({ list: () => rows, replace: value => { rows = value; }, create: vi.fn(), update });
    await expect(owner.update('a', { name: 'draft', expectedRevision: 2 })).rejects.toThrow('conflict');
    expect(update.mock.calls[0][1].expectedRevision).toBe(2);
    expect(rows[0].name).toBe('old');
  });
});

describe('confirmed folder deletion', () => {
  it('keeps the preset on rejection and fences a pre-delete list after ACK', async () => {
    let rows: FolderPreset[] = [{ id: 'a', name: 'keep', revision: 1 }];
    const remove = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const owner = createFolderPresetMutations({ list: () => rows, replace: value => { rows = value; },
      create: vi.fn(), update: vi.fn(), remove });
    await expect(owner.remove('a')).rejects.toThrow('not confirmed');
    expect(rows).toHaveLength(1);
    const started = owner.revision(); const old = rows;
    await owner.remove('a');
    expect(rows).toEqual([]);
    expect(owner.reconcile(old, started)).toBe(false);
    expect(rows).toEqual([]);
  });
});
