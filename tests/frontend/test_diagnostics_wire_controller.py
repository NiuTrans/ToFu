"""The typed Debug wire owner keeps one request and one last-good feed per open epoch."""

from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
BUNDLER = ROOT / 'scripts/vite_test_bundle.mjs'
SOURCE = ROOT / 'frontend/src/features/diagnostics-wire-controller.ts'


def test_diagnostics_wire_controller_resilience(tmp_path: Path) -> None:
    if not shutil.which('node') or not BUNDLER.is_file():
        pytest.skip('Node test bundler unavailable')
    module = tmp_path / 'diagnostics-wire-controller.cjs'
    result = subprocess.run([
        str(BUNDLER), str(SOURCE), '--bundle', '--format=cjs',
        '--platform=node', f'--outfile={module}',
    ], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    harness = tmp_path / 'wire-controller-test.cjs'
    harness.write_text(r"""
const assert = require('node:assert/strict');
const { createDiagnosticsWireController } = require(process.argv[2]);
const pending = [];
const wire = (label) => ({ messages: [{ content: label }],
  sections: [{ type: 'turn' }], turns: [{}] });
const controller = createDiagnosticsWireController((id) =>
  new Promise((resolve, reject) => pending.push({ id, resolve, reject })));
const drain = async () => { await Promise.resolve(); await Promise.resolve(); };
(async () => {
  controller.select('a');
  const first = controller.load();
  const overlapping = controller.load();
  await drain();
  assert.equal(pending.length, 1, 'a poll and retry share one request');
  pending.shift().resolve(wire('a-1'));
  assert.equal(await first, null, 'older intent must not paint');
  assert.equal((await overlapping).wire.messages[0].content, 'a-1');

  const partial = controller.load();
  await drain();
  pending.shift().resolve({ ...wire('incomplete'), readError: true });
  const prior = await partial;
  assert.equal(prior.readError, true);
  assert.equal(prior.wire.messages[0].content, 'a-1',
    'partial storage failures preserve complete evidence');

  const thrown = controller.load();
  await drain();
  pending.shift().reject(new Error('storage down'));
  assert.equal((await thrown).wire.messages[0].content, 'a-1');

  const stale = controller.load();
  await drain();
  controller.select('b');
  const next = controller.load();
  await drain();
  assert.deepEqual(pending.map((p) => p.id), ['a', 'b']);
  pending.shift().resolve(wire('a-late'));
  assert.equal(await stale, null, 'prior conversation must not paint');
  pending.shift().resolve({ ...wire('b-partial'), readError: true });
  assert.equal((await next).wire.messages[0].content, 'b-partial',
    'first partial read is shown with a warning, not borrowed from a');
  const invalid = controller.load();
  await drain();
  pending.shift().resolve({ messages: [], sections: null, turns: [] });
  assert.deepEqual(await invalid, { wire: null, readError: true });

  const badMessage = controller.load();
  await drain();
  pending.shift().resolve({ messages: [null], sections: [], turns: [] });
  assert.deepEqual(await badMessage, { wire: null, readError: true },
    'a malformed snapshot must not crash message rendering');

  const pendingClose = controller.load();
  await drain();
  controller.select(null);
  pending.shift().resolve(wire('closed'));
  assert.equal(await pendingClose, null, 'closed drawer must ignore late reads');
  assert.equal((await controller.load()).wire.messages.length, 0);
  console.log('PASS diagnostics wire lifecycle');
})().catch((error) => { console.error(error); process.exitCode = 1; });
""", encoding='utf-8')
    run = subprocess.run(['node', str(harness), str(module)], cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert 'PASS diagnostics wire lifecycle' in run.stdout
