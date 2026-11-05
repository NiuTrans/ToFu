"""Recent-project rows pass the stored path to their action as one argument.

``_renderRecentList`` (frontend/src/runtime/sections/project.js) renders each
recent project as ``data-tofu-action="selectRecentProject(...)"`` (or
``beginRelinkRecent(...)`` for a missing directory). The path used to be spliced
into a single-quoted action argument and only HTML-escaped; the browser decodes
``&#39;`` back to ``'`` before the action registry parses the expression, so a
directory name could close the string and add arguments or calls, including
other registered actions. The path is now a JSON string literal escaped as a
whole for the attribute.

The harness renders the retained section under jsdom, installs the REAL action
registry (frontend/src/action-registry.ts bundled by esbuild) and clicks the
rows, so it exercises the same parse the browser performs.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import orchestration_legacy_test_root

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
REGISTRY = os.path.join(ROOT, 'frontend', 'src', 'action-registry.ts')

CRAFTED_PATHS = [
    "/tmp/x',deleteEverything(),'",
    '/tmp/quote"and\\backslash',
    "/srv/plain project",
]

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const [SECTION, ROOT, SNAPSHOT, PATHS] = process.argv.slice(2);
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div id="recentProjectPaths">
    <span id="recentCount"></span>
    <input id="recentSearchInput">
    <button id="recentSearchClear" hidden></button>
    <div id="recentPathsList"></div>
  </div>
</body>`, { url: 'http://localhost/' });
global.window = dom.window;
global.document = dom.window.document;
global.Element = dom.window.Element;
// jsdom only accepts an AbortSignal from its own realm for the registry's
// document listeners.
global.AbortController = dom.window.AbortController;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.getConvById = () => null;
global.getActiveConv = () => null;

let src = fs.readFileSync(SECTION, 'utf8');
src += '\n;globalThis.__seedRecent = (rows) => { _recentProjects = rows; };';
eval(src);

(async () => {
  const registry = await import(pathToFileURL(SNAPSHOT).href);
  const calls = [];
  const injected = [];
  registry.installActionRegistry((name) => {
    if (name === 'selectRecentProject') return (...args) => calls.push(['select', args]);
    if (name === 'beginRelinkRecent') return (...args) => calls.push(['relink', args]);
    if (name === 'deleteEverything') return () => injected.push(true);
    return undefined;
  });
  const paths = JSON.parse(PATHS);
  globalThis.__seedRecent([
    ...paths.map((p) => ({ path: p, count: 1 })),
    ...paths.map((p) => ({ path: p, count: 1, exists: false })),
  ]);
  _filterRecentProjects('');
  for (const row of document.querySelectorAll('.recent-path-item')) {
    row.querySelector('.recent-path-name').click();
  }
  console.log(JSON.stringify({ calls, injected: injected.length }));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def test_recent_project_actions_receive_the_exact_path_and_run_nothing_else(tmp_path):
    require_node_modules('jsdom', 'esbuild')
    snapshot = tmp_path / 'action_registry.mjs'
    subprocess.run([
        'node', '--input-type=module', '-e',
        "import { buildSync } from 'esbuild'; buildSync({"
        "entryPoints:[process.argv[1]], outfile:process.argv[2],"
        "bundle:true, format:'esm', platform:'browser'});",
        REGISTRY, str(snapshot),
    ], cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
    harness = tmp_path / 'recent_actions_harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')

    proc = subprocess.run(
        ['node', str(harness),
         os.path.join(orchestration_legacy_test_root(), 'static', 'js', 'project.js'), ROOT,
         str(snapshot), json.dumps(CRAFTED_PATHS)],
        capture_output=True, text=True, timeout=60)

    assert proc.returncode == 0, proc.stderr
    outcome = json.loads(proc.stdout.strip().splitlines()[-1])
    assert outcome['injected'] == 0, 'a directory name invoked another registered action'
    assert outcome['calls'] == (
        [['select', [path]] for path in CRAFTED_PATHS]
        + [['relink', [path]] for path in CRAFTED_PATHS])
