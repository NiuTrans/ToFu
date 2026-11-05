/** Exercise the shipped bundler policy through independently loaded entries. */
const assert = require('node:assert/strict');
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const test = require('node:test');
const { rollup } = require('rollup');

test('standalone entries reuse shared modules without starting conversation presentation', async () => {
  const { default: config } = await import('../vite.config.mjs');
  const root = await mkdtemp(path.join(tmpdir(), 'tofu-chunk-ownership-'));
  const source = path.join(root, 'frontend/src');
  const destination = path.join(root, 'output');
  let bundle;
  try {
    await mkdir(path.join(source, 'conversation/presentation'), { recursive: true });
    const files = {
      'shared.js': `
        export const effects = ['shared'];
        export const identity = {};
        export function translate(value) { return 'translated:' + value; }
      `,
      'conversation/presentation/tool.js': `
        import { effects, translate } from '../../shared.js';
        effects.push('presentation');
        export function render() { return translate('tool'); }
      `,
      'application.js': `
        import { effects, identity } from './shared.js';
        import { render } from './conversation/presentation/tool.js';
        effects.push('application');
        export { effects, identity, render };
      `,
      'admin.js': `
        import { effects, identity, translate } from './shared.js';
        effects.push('admin');
        export { effects, identity };
        export const label = translate('admin');
      `,
      'plugin.js': `
        import { effects, identity, translate } from './shared.js';
        effects.push('plugin');
        export { effects, identity };
        export const label = translate('plugin');
      `,
    };
    await Promise.all(Object.entries(files).map(([file, body]) =>
      writeFile(path.join(source, file), body)));
    bundle = await rollup({
      input: Object.fromEntries(['application', 'admin', 'plugin'].map(name =>
        [name, path.join(source, `${name}.js`)])),
    });
    await bundle.write({
      ...config.build.rollupOptions.output,
      dir: destination,
      format: 'es',
      entryFileNames: '[name].mjs',
      chunkFileNames: '[name]-[hash].mjs',
    });
    const load = name => import(pathToFileURL(path.join(destination, `${name}.mjs`)).href);
    const admin = await load('admin');
    assert.equal(admin.label, 'translated:admin');
    assert.deepEqual(admin.effects, ['shared', 'admin']);
    const plugin = await load('plugin');
    assert.equal(plugin.label, 'translated:plugin');
    assert.equal(plugin.identity, admin.identity);
    assert.deepEqual(plugin.effects, ['shared', 'admin', 'plugin']);
    const application = await load('application');
    assert.equal(application.identity, admin.identity);
    assert.equal(application.render(), 'translated:tool');
    assert.deepEqual(application.effects, ['shared', 'admin', 'plugin', 'presentation', 'application']);
  } finally {
    try {
      await bundle?.close();
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  }
});
