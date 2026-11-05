// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from 'vitest';
import { mountPluginPage } from './page';
import type { PluginHostV1 } from './ports';

const host = { version: 1 } as PluginHostV1;

beforeEach(() => {
  window.history.replaceState({}, '', '/proxy/15000/trading.html');
  document.body.innerHTML = '<div data-tofu-plugin-error hidden>Load failed</div>';
  document.body.dataset.tofuPluginModule = 'trading-static/js/trading/entry.js';
});

it('resolves against the page mount and awaits plugin initialization', async () => {
  let finish!: () => void;
  const pending = new Promise<void>(resolve => { finish = resolve; });
  const mount = vi.fn(() => pending);
  const load = vi.fn(async () => ({ mount }));
  const work = mountPluginPage(document, host, load);
  await Promise.resolve();
  expect(load).toHaveBeenCalledWith(new URL('/proxy/15000/trading-static/js/trading/entry.js', document.URL).href);
  expect(mount).toHaveBeenCalledWith(host);
  expect(document.body.dataset.tofuPluginState).toBe('loading');
  finish();
  await work;
  expect(document.body.dataset.tofuPluginState).toBe('ready');
  expect(document.querySelector<HTMLElement>('[data-tofu-plugin-error]')!.hidden).toBe(true);
});

it.each(['../outside.js', '/outside.js', 'https://other.test/plugin.js'])('rejects a module outside the mount: %s', async source => {
  document.body.dataset.tofuPluginModule = source;
  const load = vi.fn();
  await expect(mountPluginPage(document, host, load)).rejects.toThrow('leaves the page mount');
  expect(load).not.toHaveBeenCalled();
  expect(document.body.dataset.tofuPluginState).toBe('error');
  expect(document.querySelector<HTMLElement>('[data-tofu-plugin-error]')!.hidden).toBe(false);
});

it('keeps import failures visible and never invokes a plugin', async () => {
  const failure = new Error('module transport failed');
  await expect(mountPluginPage(document, host, async () => { throw failure; })).rejects.toBe(failure);
  expect(document.body.dataset.tofuPluginState).toBe('error');
  expect(document.querySelector<HTMLElement>('[data-tofu-plugin-error]')!.hidden).toBe(false);
});

it('keeps plugin initialization failures visible', async () => {
  const failure = new Error('plugin failed');
  await expect(mountPluginPage(document, host, async () => ({ mount: async () => { throw failure; } }))).rejects.toBe(failure);
  expect(document.body.dataset.tofuPluginState).toBe('error');
});
