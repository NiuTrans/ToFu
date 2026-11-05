/** Prior generation retention follows its own complete published entry graph. */
const assert = require('node:assert/strict');
const { mkdtemp, mkdir, rm, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const path = require('node:path');
const test = require('node:test');

test('a newly required entry cannot unpin an intact previous generation', async () => {
  const { createViteArtifactValidator } = await import('../scripts/vite_asset_manifest.mjs');
  const root = await mkdtemp(path.join(tmpdir(), 'tofu-previous-graph-'));
  try {
    await mkdir(path.join(root, 'assets'));
    const manifest = {
      'main.ts': { file: 'assets/main.js', isEntry: true, dynamicImports: ['old-feature.ts'],
        css: ['assets/main.css'], assets: ['assets/font.woff2'] },
      'old-feature.ts': { file: 'assets/old-feature.js', imports: ['shared'] },
      shared: { file: 'assets/shared.js', imports: ['old-feature.ts'] },
      worker: { file: 'assets/worker.js' },
    };
    const files = ['assets/main.js', 'assets/old-feature.js', 'assets/shared.js',
      'assets/worker.js', 'assets/main.css', 'assets/font.woff2'];
    await Promise.all(files.map(file => writeFile(path.join(root, file), 'asset')));
    await writeFile(path.join(root, 'manifest.json'), JSON.stringify(manifest));
    const validator = createViteArtifactValidator(['main.ts', 'new-plugin.ts']);
    const previous = await validator.readPrevious(root);
    assert.deepEqual(previous.manifest, manifest);
    assert.deepEqual([...previous.assets].sort(), files.slice().sort());
    await assert.rejects(validator.validateCandidate(manifest, root), /no entry: new-plugin.ts/);
    manifest['new-plugin.ts'] = { file: 'assets/plugin.js', isEntry: true };
    await writeFile(path.join(root, 'assets/plugin.js'), 'export {};');
    assert.deepEqual([...await validator.validateCandidate(manifest, root)].sort(),
      [...files, 'assets/plugin.js'].sort());
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

for (const [name, change] of [
  ['missing asset', manifest => { manifest['main.ts'].file = 'assets/missing.js'; }],
  ['missing import', manifest => { manifest['main.ts'].imports = ['missing']; }],
  ['malformed imports', manifest => { manifest['main.ts'].imports = 'shared'; }],
  ['escaping path', manifest => { manifest['main.ts'].file = 'assets/../outside.js'; }],
  ['backslash path', manifest => { manifest['main.ts'].file = 'assets\\outside.js'; }],
  ['no declared entry', manifest => { manifest['main.ts'].isEntry = false; }],
  ['array root', () => []],
]) {
  test(`incomplete or unsafe previous graph is refused: ${name}`, async () => {
    const { createViteArtifactValidator } = await import('../scripts/vite_asset_manifest.mjs');
    const root = await mkdtemp(path.join(tmpdir(), 'tofu-invalid-graph-'));
    try {
      await mkdir(path.join(root, 'assets'));
      await writeFile(path.join(root, 'assets/main.js'), 'export {};');
      let manifest = { 'main.ts': { file: 'assets/main.js', isEntry: true } };
      manifest = change(manifest) || manifest;
      await writeFile(path.join(root, 'manifest.json'), JSON.stringify(manifest));
      const validator = createViteArtifactValidator(['main.ts']);
      assert.deepEqual(await validator.readPrevious(root), { manifest: null, assets: new Set() });
      await assert.rejects(validator.validateCandidate(manifest, root));
    } finally {
      await rm(root, { recursive: true, force: true });
    }
  });
}
