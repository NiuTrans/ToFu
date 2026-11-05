/* Locked browser dependencies must reach every shipped standalone surface. */
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { mkdtemp, mkdir, readFile, writeFile, rm } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { execFileSync } = require('node:child_process');
const { test } = require('node:test');

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'tofu-vendor-assets-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const files = {
    'package-lock.json': JSON.stringify({ packages: { 'node_modules/katex': { version: '1.2.3' } } }),
    'node_modules/katex/package.json': JSON.stringify({ version: '1.2.3' }),
    'node_modules/katex/dist/katex.min.js': 'renderer bytes',
    'node_modules/katex/dist/katex.min.css': '@font-face{src:url(fonts/Test.woff2)}',
    'node_modules/katex/dist/fonts/Test.woff2': 'font bytes',
    'node_modules/katex/dist/contrib/auto-render.min.js': 'auto render bytes',
    'node_modules/katex/LICENSE': 'upstream license',
    'frontend/src/artifacts/math-auto-render.js': 'authored artifact adapter',
  };
  for (const [name, content] of Object.entries(files)) {
    await mkdir(join(root, name, '..'), { recursive: true });
    await writeFile(join(root, name), content);
  }
  return root;
}

async function owner() {
  return (await import('../scripts/frontend_vendor_assets.mjs')).syncFrontendVendorAssets;
}

test('writes one locked version, exact assets and executable export metadata', async (t) => {
  const root = await fixture(t);
  const sync = await owner();
  await sync({ root, write: true });
  assert.deepEqual(await readFile(join(root, 'static/vendor/katex/katex.min.js')),
    await readFile(join(root, 'node_modules/katex/dist/katex.min.js')));
  assert.equal(await readFile(join(root, 'static/vendor/katex/fonts/Test.woff2'), 'utf8'), 'font bytes');
  const metadata = JSON.parse(execFileSync('python3', ['-c',
    'import json,runpy,sys; m=runpy.run_path(sys.argv[1]); print(json.dumps([m["KATEX_VERSION"],m["KATEX_CDN_ASSETS"],m["KATEX_STATIC_SHA256"]]))',
    join(root, 'lib/http_transport/generated_frontend_vendor.py')], { encoding: 'utf8' }));
  assert.equal(metadata[0], '1.2.3');
  assert.equal(metadata[1].renderer.url, 'https://cdn.jsdelivr.net/npm/katex@1.2.3/dist/katex.min.js');
  assert.equal(metadata[1].renderer.integrity,
    'sha384-' + createHash('sha384').update('renderer bytes').digest('base64'));
  assert.equal(metadata[2]['static/vendor/katex/katex.min.js'],
    createHash('sha256').update('renderer bytes').digest('hex'));
  await sync({ root, write: false });
});

test('check detects drift without repairing it; generation preserves unowned files', async (t) => {
  const root = await fixture(t);
  const sync = await owner();
  await sync({ root, write: true });
  const path = join(root, 'static/vendor/katex/katex.min.js');
  await writeFile(path, 'drift');
  const unowned = join(root, 'static/vendor/katex/unowned.txt');
  await writeFile(unowned, 'keep');
  await assert.rejects(sync({ root, write: false }), /katex\.min\.js/);
  assert.equal(await readFile(path, 'utf8'), 'drift');
  await sync({ root, write: true });
  assert.equal(await readFile(unowned, 'utf8'), 'keep');
});

test('refuses installed-version drift before replacing any output', async (t) => {
  const root = await fixture(t);
  const sync = await owner();
  await sync({ root, write: true });
  const before = await readFile(join(root, 'lib/http_transport/generated_frontend_vendor.py'));
  await writeFile(join(root, 'node_modules/katex/package.json'), JSON.stringify({ version: '9.9.9' }));
  await assert.rejects(sync({ root, write: true }), /lock/i);
  assert.deepEqual(await readFile(join(root, 'lib/http_transport/generated_frontend_vendor.py')), before);
});

test('rejects missing or escaped font references before writing outputs', async (t) => {
  const root = await fixture(t);
  const sync = await owner();
  await sync({ root, write: true });
  const before = await readFile(join(root, 'static/vendor/katex/katex.min.css'));
  for (const url of ['fonts/Missing.woff2', '../secret.woff2', 'https://example.com/font.woff2']) {
    await writeFile(join(root, 'node_modules/katex/dist/katex.min.css'), `a{src:url(${url})}`);
    await assert.rejects(sync({ root, write: true }));
    assert.deepEqual(await readFile(join(root, 'static/vendor/katex/katex.min.css')), before);
  }
});
