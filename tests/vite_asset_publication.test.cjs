/** Actual filesystem publication stays behind graph, budget and boot admission. */
const assert = require('node:assert/strict');
const { mkdtemp, mkdir, readFile, rm, stat, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const path = require('node:path');
const test = require('node:test');

async function fixture(run) {
  const { createViteArtifactValidator } = await import('../scripts/vite_asset_manifest.mjs');
  const { publishViteGeneration } = await import('../scripts/vite_asset_publication.mjs');
  const root = await mkdtemp(path.join(tmpdir(), 'tofu-publication-'));
  const liveDir = path.join(root, 'live');
  const artifactDir = path.join(root, 'candidate');
  try {
    await Promise.all([liveDir, artifactDir].map(dir => mkdir(path.join(dir, 'assets'), { recursive: true })));
    const previousManifest = { main: { file: 'assets/old.js', isEntry: true } };
    const manifest = { main: { file: 'assets/new.js', isEntry: true },
      plugin: { file: 'assets/plugin.js', isEntry: true } };
    const before = JSON.stringify(previousManifest);
    await writeFile(path.join(liveDir, 'manifest.json'), before);
    await writeFile(path.join(liveDir, 'assets/old.js'), 'previous');
    await writeFile(path.join(artifactDir, 'assets/new.js'), 'next');
    await writeFile(path.join(artifactDir, 'assets/plugin.js'), 'plugin');
    const events = [];
    async function assertUnpublished() {
      assert.equal(await readFile(path.join(liveDir, 'manifest.json'), 'utf8'), before);
      assert.equal(await readFile(path.join(liveDir, 'assets/old.js'), 'utf8'), 'previous');
    }
    async function check(kind) {
      await assertUnpublished();
      await assert.rejects(stat(path.join(liveDir, 'assets/new.js')), { code: 'ENOENT' });
      events.push(kind);
    }
    const options = { manifest, previousManifest, artifactDir, liveDir,
      validator: createViteArtifactValidator(['main', 'plugin']),
      checkBudget: () => check('budget'), checkBoot: () => check('boot') };
    await run({ options, publishViteGeneration, assertUnpublished, events });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

test('all admitted assets precede the manifest commit and the previous graph survives', async () => {
  await fixture(async ({ options, publishViteGeneration, events }) => {
    const assets = await publishViteGeneration(options);
    assert.deepEqual(events, ['budget', 'boot']);
    assert.deepEqual([...assets].sort(), ['assets/new.js', 'assets/plugin.js']);
    assert.deepEqual(JSON.parse(await readFile(path.join(options.liveDir, 'manifest.json'))), options.manifest);
    assert.deepEqual(JSON.parse(await readFile(path.join(options.liveDir, 'previous-manifest.json'))), options.previousManifest);
    assert.equal(await readFile(path.join(options.liveDir, 'assets/new.js'), 'utf8'), 'next');
    assert.equal(await readFile(path.join(options.liveDir, 'assets/plugin.js'), 'utf8'), 'plugin');
    assert.equal(await readFile(path.join(options.liveDir, 'assets/old.js'), 'utf8'), 'previous');
  });
});

for (const phase of ['graph', 'budget', 'boot', 'copy']) {
  test(`failed ${phase} leaves the serving manifest and old assets intact`, async () => {
    await fixture(async ({ options, publishViteGeneration, assertUnpublished, events }) => {
      const failure = new Error(`refused ${phase}`);
      if (phase === 'graph') delete options.manifest.plugin;
      if (phase === 'budget') options.checkBudget = () => { throw failure; };
      if (phase === 'boot') options.checkBoot = () => { throw failure; };
      if (phase === 'copy') options.checkBoot = async () => {
        events.push('boot');
        await rm(path.join(options.artifactDir, 'assets/plugin.js'));
      };
      await assert.rejects(publishViteGeneration(options), error => {
        if (phase === 'graph') return /no entry: plugin/.test(error.message);
        if (phase === 'copy') return error.code === 'ENOENT';
        return error === failure;
      });
      await assertUnpublished();
      assert.deepEqual(events, phase === 'copy' ? ['budget', 'boot'] : phase === 'boot' ? ['budget'] : []);
    });
  });
}

test('concurrent publications commit complete graphs without sharing a temporary manifest', async () => {
  await fixture(async ({ options, publishViteGeneration }) => {
    let ready = 0;
    let release;
    const admitted = new Promise(resolve => { release = resolve; });
    options.checkBudget = async () => {};
    options.checkBoot = async () => {
      if (++ready === 2) release();
      await admitted;
    };
    const publish = () => publishViteGeneration(options).catch(error => {
      release();
      throw error;
    });
    const outcomes = await Promise.allSettled([publish(), publish()]);
    assert.deepEqual(outcomes.map(result => result.status), ['fulfilled', 'fulfilled']);
    assert.deepEqual(JSON.parse(await readFile(path.join(options.liveDir, 'manifest.json'))), options.manifest);
    for (const asset of ['assets/new.js', 'assets/plugin.js', 'assets/old.js']) {
      assert.ok((await stat(path.join(options.liveDir, asset))).isFile());
    }
  });
});

test('publishing the same content-addressed assets again does not replace them', async () => {
  await fixture(async ({ options, publishViteGeneration }) => {
    await publishViteGeneration(options);
    const asset = path.join(options.liveDir, 'assets/new.js');
    const before = await stat(asset, { bigint: true });
    await publishViteGeneration({ ...options, previousManifest: options.manifest,
      checkBudget: async () => {}, checkBoot: async () => {} });
    const after = await stat(asset, { bigint: true });
    assert.equal(after.ino, before.ino);
    assert.equal(after.mtimeNs, before.mtimeNs);
    assert.equal(await readFile(asset, 'utf8'), 'next');
  });
});
