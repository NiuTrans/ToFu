/** Ordered CSS authority, deferred ownership and generated-entry drift contracts. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'tofu-style-composition-'));
  t.after(() => rmSync(root, { recursive: true }));
  const write = (path, content) => {
    const file = join(root, path); mkdirSync(require('node:path').dirname(file), { recursive: true });
    writeFileSync(file, content);
  };
  const application = {
    version: 1, output: 'static/styles.css', sections: [{ path: 'core.css' }],
    deferredSections: [{ path: 'late.css', featureEntry: 'frontend/src/features/late.ts' }],
  };
  const settings = {
    version: 1, output: 'static/settings.css',
    sections: [{ path: 'second.css' }, { path: 'first.css' }],
    sourceEntry: 'settings.generated.css', featureEntry: 'frontend/src/features/settings.ts',
  };
  const manifest = () => {
    write('frontend/src/styles/application/manifest.json', JSON.stringify(application));
    write('frontend/src/styles/settings/manifest.json', JSON.stringify(settings));
  };
  manifest();
  write('frontend/src/styles/application/core.css', '.core{color:blue}');
  write('frontend/src/styles/application/late.css', '.late{color:red}');
  write('frontend/src/styles/settings/first.css', '.settings{color:blue}');
  write('frontend/src/styles/settings/second.css', '.settings{color:red}');
  write('frontend/src/features/late.ts', "import '../styles/application/late.css';");
  write('frontend/src/features/settings.ts', "import '../styles/settings/settings.generated.css';");
  mkdirSync(join(root, 'static'));
  return { root, write, application, settings, manifest };
}

async function composer() { return (await import('../scripts/compose_frontend_styles.mjs')).composeStyles; }

test('keeps deferred rules out of critical CSS and preserves declared lazy cascade order', async t => {
  const f = fixture(t); const compose = await composer();
  await compose({ projectRoot: f.root });
  assert.equal(readFileSync(join(f.root, 'static/styles.css'), 'utf8'), '.core{color:blue}');
  assert.equal(readFileSync(join(f.root, 'static/settings.css'), 'utf8'), '.settings{color:red}.settings{color:blue}');
  const entry = readFileSync(join(f.root, 'frontend/src/styles/settings/settings.generated.css'), 'utf8');
  assert.ok(entry.indexOf('second.css') < entry.indexOf('first.css'));
  await compose({ check: true, projectRoot: f.root });
});

test('rejects critical/deferred double ownership before publishing output', async t => {
  const f = fixture(t); f.application.sections.push({ path: 'late.css' }); f.manifest();
  await assert.rejects((await composer())({ projectRoot: f.root }), /duplicate stylesheet section/);
});

test('rejects missing or type-only feature imports instead of silently dropping a style', async t => {
  const f = fixture(t);
  const compose = await composer();
  for (const source of ['export const late = true;', "import type {} from '../styles/application/late.css';"]) {
    f.write('frontend/src/features/late.ts', source);
    await assert.rejects(compose({ projectRoot: f.root }), /must statically import/);
  }
});

test('rejects unsafe generated entries and source overwrites', async t => {
  const f = fixture(t); const compose = await composer();
  f.settings.sourceEntry = '../settings.generated.css'; f.manifest();
  await assert.rejects(compose({ projectRoot: f.root }), /escapes its declared root/);
  f.settings.sourceEntry = 'first.css'; f.manifest();
  await assert.rejects(compose({ projectRoot: f.root }), /separate .generated.css/);
});

test('detects stale generated imports when manifest order changes', async t => {
  const f = fixture(t); const compose = await composer();
  await compose({ projectRoot: f.root });
  f.settings.sections.reverse(); f.manifest();
  await assert.rejects(compose({ check: true, projectRoot: f.root }), /settings.generated.css.*stale/);
});

test('rejects malformed frozen-copy metadata before publishing CSS', async t => {
  const f = fixture(t);
  f.application.sections[0].legacyTokenSha256 = 'not-a-digest'; f.manifest();
  await assert.rejects((await composer())({ projectRoot: f.root }), /legacyTokenSha256/);
  assert.throws(() => readFileSync(join(f.root, 'static/styles.css')), /ENOENT/);
});
