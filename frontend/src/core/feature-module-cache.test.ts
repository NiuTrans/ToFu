/** CSS and JS form one graph transaction for every stable feature loader. */
import { describe, expect, it } from 'vitest';
import { createFeatureModuleCache } from './feature-module-cache';

describe('document feature graph transactions', () => {
  it('shares a pending and fulfilled graph across callers', async () => {
    const load = createFeatureModuleCache();
    let calls = 0;
    let resolve!: (value: { ready: boolean }) => void;
    const loader = () => { calls += 1; return new Promise<{ ready: boolean }>(done => { resolve = done; }); };
    const first = load(loader);
    expect(load(loader)).toBe(first);
    await Promise.resolve();
    expect(calls).toBe(1);
    const module = { ready: true };
    resolve(module);
    expect(await first).toBe(module);
    expect(await load(loader)).toBe(module);
    expect(calls).toBe(1);
  });

  it('retains a failed CSS graph so another click cannot bypass the preload failure', async () => {
    const load = createFeatureModuleCache();
    let calls = 0;
    const error = new Error('Unable to preload CSS for settings.css');
    const loader = () => { calls += 1; return Promise.reject(error); };
    const first = load(loader);
    await expect(first).rejects.toBe(error);
    expect(load(loader)).toBe(first);
    await expect(load(loader)).rejects.toBe(error);
    expect(calls).toBe(1);
    // A new document owns a fresh cache, just as it owns a fresh Vite preload set.
    await expect(createFeatureModuleCache()(loader)).rejects.toBe(error);
    expect(calls).toBe(2);
  });

  it('does not let an unrelated feature inherit a failed graph', async () => {
    const load = createFeatureModuleCache();
    await expect(load(() => Promise.reject(new Error('Broken module')))).rejects.toThrow('Broken module');
    expect(await load(() => Promise.resolve('Other feature'))).toBe('Other feature');
  });
});
