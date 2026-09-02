"""Executable contracts for the typed project-folder browse coordinator."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'frontend/src/core/project-browse-coordinator.ts'


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
