/** Content dependency contracts reject facade re-entry and cycles. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');

async function check(t, sources, scope) {
  const root = mkdtempSync(join(tmpdir(), 'tofu-renderer-boundary-'));
  t.after(() => rmSync(root, { recursive: true }));
  mkdirSync(join(root, 'renderers'));
  for (const [name, source] of Object.entries(sources)) writeFileSync(join(root, 'renderers', name), source);
  const { checkRendererDependencies } = await import('../scripts/check_conversation_renderer_boundaries.mjs');
  return checkRendererDependencies(root, {
    conversationRendererModules: 'renderers',
    conversationRendererComposition: 'facade.ts',
  }, scope);
}

test('accepts an acyclic graph with external ports', async t => {
  const failures = await check(t, {
    'text.ts': "import type { Ports } from '../ports'; import { copy } from './copy';",
    'copy.ts': 'export const copy = value => value;',
  });
  assert.deepEqual(failures, []);
});

test('rejects static and lazy composition imports, including type re-exports', async t => {
  const failures = await check(t, {
    'text.ts': "import type { Ports } from '../facade';",
    'copy.ts': "export type { Ports } from '../facade';",
    'media.ts': "const load = () => import('../facade');",
  });
  assert.equal(failures.length, 3);
  assert.ok(failures.every(failure => failure.includes('composition facade')));
});

test('rejects cycles through shared helpers with an actionable path', async t => {
  const failures = await check(t, {
    'text.ts': "import { copy } from './copy';",
    'copy.ts': "import { text } from './text';",
  });
  assert.equal(failures.length, 1);
  assert.match(failures[0], /copy\.ts.*text\.ts.*copy\.ts/);
});

test('does not let an empty owner or a hidden helper cycle pass', async t => {
  assert.match((await check(t, {}))[0], /no source modules/);
  const failures = await check(t, {
    'entry.ts': 'export const entry = true;',
    'orphan.ts': "import './orphan';",
  });
  assert.match(failures[0], /orphan\.ts.*orphan\.ts/);
});

test('checks projection type dependencies with the owning scope in diagnostics', async t => {
  const failures = await check(t, {
    'types.ts': "import type { Turn } from './blocks';",
    'blocks.ts': "import type { Block } from './types'; export type { VM } from '../facade';",
  }, 'View model');
  assert.equal(failures.length, 2);
  assert.ok(failures.some(failure => failure.includes('composition facade')));
  assert.ok(failures.some(failure => failure.startsWith('View model dependency cycle:')));
});

test('includes declared external collaborators in the projection boundary', async t => {
  const root = mkdtempSync(join(tmpdir(), 'tofu-projection-boundary-'));
  t.after(() => rmSync(root, { recursive: true }));
  mkdirSync(join(root, 'projection'));
  writeFileSync(join(root, 'projection', 'blocks.ts'), "import { anchor } from '../anchor';");
  writeFileSync(join(root, 'anchor.ts'), "import type { Block } from './facade';");
  const { checkRendererDependencies } = await import('../scripts/check_conversation_renderer_boundaries.mjs');
  const failures = checkRendererDependencies(root, {
    conversationRendererModules: 'projection', conversationRendererComposition: 'facade.ts',
  }, 'View model', ['anchor.ts']);
  assert.equal(failures.length, 1);
  assert.match(failures[0], /anchor\.ts.*composition facade/);
});

test('Surface accepts typed appearance ports and rejects design leakage into reconciliation', async t => {
  const root = mkdtempSync(join(tmpdir(), 'tofu-surface-boundary-'));
  t.after(() => rmSync(root, { recursive: true }));
  const owners = { surfaceEngine: 'engine.ts', surfaceTurnRendering: 'turn.ts',
    conversationDom: 'facade.ts', surfaceDesign: 'design.ts', surfaceDefaultRendering: 'fallback.ts' };
  const { checkSurfaceDependencies } = await import('../scripts/check_conversation_renderer_boundaries.mjs');
  writeFileSync(join(root, 'turn.ts'), 'export const turn = true;');
  writeFileSync(join(root, 'engine.ts'), "import type { Design } from './design-contracts'; design.decorate(node, 'turn'); node.dataset.turnId = id;");
  assert.deepEqual(checkSurfaceDependencies(root, owners), []);
  for (const source of [
    "import './surface.css';", "export { design } from './design';",
    "const load = () => import('./fallback.js');", "import type { Surface } from './facade';",
    "node.className = 'turn';", "node['style'].color = 'red';",
    "node.classList.add('turn');", "node.textContent = 'Loading';",
    "node.setAttribute('class', 'turn');",
  ]) {
    writeFileSync(join(root, 'engine.ts'), source);
    const failures = checkSurfaceDependencies(root, owners);
    assert.ok(failures.length > 0, source);
    assert.ok(failures.every(failure => failure.includes('Surface design port')));
  }
});
