"""Executable contracts for the typed project-folder browse coordinator."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'frontend/src/core/project-browse-coordinator.ts'
DIRECTORY_BROWSER_SOURCE = (
    ROOT / 'frontend/src/features/project/directory-browser.ts')


def test_browse_coordinator_caches_canonical_aliases_and_cancels_stale_reads():
    script = f"""
const fs = require('fs');
const ts = require('typescript');
const assert = require('assert');
const source = fs.readFileSync({json.dumps(str(SOURCE))}, 'utf8');
const compiled = ts.transpileModule(source, {{ compilerOptions: {{
  target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.CommonJS,
  strict: true,
}} }}).outputText;
const modulePort = {{ exports: {{}} }};
new Function('module', 'exports', compiled)(modulePort, modulePort.exports);
const {{ createProjectBrowseCoordinator }} = modulePort.exports;

(async () => {{
  const values = new Map();
  const storage = {{
    getItem: (key) => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  }};
  let now = 1_000;
  const coordinator = createProjectBrowseCoordinator(() => storage, () => now);
  const data = (path, name) => ({{
    path,
    dirs: [{{ path: `${{path}}/${{name}}`, name, itemCount: 2 }}],
    parent: null,
    filesCount: 3,
    truncated: false,
  }});

  const first = coordinator.load('~', false, async () => data('/home/me', 'one'));
  assert.strictEqual(first.cached, null);
  assert.strictEqual((await first.completion).kind, 'success');

  now += 1;
  const cached = coordinator.load('~', false, async () => data('/home/me', 'two'));
  assert.strictEqual(cached.cached.path, '/home/me');
  assert.strictEqual(cached.cached.dirs[0].name, 'one');
  assert.strictEqual((await cached.completion).kind, 'success');
  coordinator.invalidate('/home/me');
  const invalidated = coordinator.load('~', false, async () => data('/home/me', 'three'));
  assert.strictEqual(invalidated.cached, null);
  await invalidated.completion;

  let resolveSlow;
  let slowSignal;
  const slow = coordinator.load('/slow', false, (signal) => {{
    slowSignal = signal;
    return new Promise((resolve) => {{ resolveSlow = resolve; }});
  }});
  const fast = coordinator.load(
    '/fast', false, async () => data('/fast', 'winner'));
  assert.strictEqual(slowSignal.aborted, true);
  resolveSlow(data('/slow', 'stale'));
  assert.strictEqual((await slow.completion).kind, 'cancelled');
  assert.strictEqual((await fast.completion).kind, 'success');
  process.stdout.write('ok');
}})().catch((error) => {{
  console.error(error);
  process.exitCode = 1;
}});
"""
    result = subprocess.run(
        ['node', '-e', script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'ok'


def test_directory_browser_owns_bounded_filter_and_escaped_actions(tmp_path):
    compiled = tmp_path / 'directory-browser.cjs'
    build = subprocess.run(
        [
            str(ROOT / 'node_modules/.bin/esbuild'),
            str(DIRECTORY_BROWSER_SOURCE),
            '--bundle',
            '--platform=node',
            '--format=cjs',
            f'--outfile={compiled}',
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    script = r"""
const assert = require('assert');
const { createProjectDirectoryBrowser } = require(process.argv[1]);
const escapeHtml = (value) => String(value)
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;')
  .replaceAll("'", '&#39;');
const translate = (key, values = {}) => `${key}:${values.q || values.n || ''}`;
const browser = createProjectDirectoryBrowser({
  escapeHtml,
  translate,
  assets: {
    codeFolder: '<code-icon>', plainFolder: '<folder-icon>',
    deleteFolder: '<delete-icon>', addFolder: '<add-icon>',
    folderChevron: '<chevron-icon>',
  },
});
const dangerousPath = '/root/quote\'"<&';
const data = {
  filesCount: 1,
  truncated: true,
  dirs: [
    {path:'/root/Alpha', name:'Alpha', hasCode:true, hidden:false, itemCount:101},
    {path:dangerousPath, name:'quote\'"<&', hasCode:false, hidden:true, itemCount:1},
  ],
};

assert.strictEqual(browser.setFilter('  ALPHA  '), '  ALPHA  ');
let html = browser.render(data, ['/root/Alpha']);
assert.ok(html.includes('folder-added'));
assert.ok(html.includes('100+'));
assert.ok(!html.includes('pm.showingFirst'));
assert.strictEqual(browser.resetForNavigation('/root', '/root'), false);
assert.strictEqual(browser.filterValue(), '  ALPHA  ');
assert.strictEqual(browser.resetForNavigation('/root', '/next'), true);
assert.strictEqual(browser.filterValue(), '');

html = browser.render(data, []);
assert.ok(html.includes('&quot;'));
assert.ok(html.includes('&amp;'));
assert.ok(html.includes('pm.showingFirst:2'));
assert.ok(!html.includes('data-tofu-action="browseDirectory("/root'));
browser.setFilter('x'.repeat(300));
assert.strictEqual(browser.filterValue().length, 256);
browser.setFilter('absent');
assert.ok(browser.render(data, []).includes('pm.browseNoMatches:absent'));
browser.clearFilter();
assert.ok(browser.render({dirs:[], filesCount:3, truncated:false}, [])
  .includes('pm.filesCount:3'));
process.stdout.write('ok');
"""
    result = subprocess.run(
        ['node', '-e', script, str(compiled)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'ok'
