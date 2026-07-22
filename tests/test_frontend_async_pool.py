"""Ground-truth for the reconnect thundering-herd fix: core/async_pool.js's
runWithConcurrency must never let more than `limit` workers run at once.

WHY (see JOURNAL 2026-07-19 overnight slow-recovery): a tab slept with 500+
conversations fires ALL N reconnect fetch/SSE calls the instant it wakes
(visibilitychange / online), saturating the event loop and the proxy. The
front-end half of the fix caps that fan-out. This test drives the REAL shipped
runWithConcurrency under node and asserts:
  (a) peak concurrency never exceeds the limit,
  (b) every item is still processed exactly once,
  (c) a throwing worker does not abort the pool (all others still run),
and a NEUTER (cap raised to Infinity) makes the concurrency assertion fail —
proving the cap is load-bearing, not incidental.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/async_pool.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A worker that stays "in flight" across a microtask/timer gap so the pool's
// real peak concurrency is observable.
function makeWorker(state) {
  return function (item) {
    state.active++;
    if (state.active > state.peak) state.peak = state.active;
    state.processed.push(item);
    return new Promise((resolve) => setTimeout(() => { state.active--; resolve(); }, 5));
  };
}

(async () => {
  const N = 50;
  const items = Array.from({ length: N }, (_, i) => i);

  // (a)+(b): cap at 4 — peak must stay <= 4 and all 50 processed once.
  const s = { active: 0, peak: 0, processed: [] };
  const res = await runWithConcurrency(items, makeWorker(s), 4);
  check('peak_within_limit', s.peak <= 4);
  check('peak_reached_limit', s.peak === 4);          // it actually saturates
  check('all_processed', s.processed.length === N);
  check('each_once', new Set(s.processed).size === N);
  check('completed_count', res.completed === N);

  // (c): a throwing worker must not abort the pool.
  const s2 = { active: 0, peak: 0, processed: [] };
  const throwing = function (item) {
    s2.active++;
    if (s2.active > s2.peak) s2.peak = s2.active;
    s2.processed.push(item);
    return new Promise((resolve, reject) => setTimeout(() => {
      s2.active--;
      if (item % 7 === 0) reject(new Error('boom ' + item)); else resolve();
    }, 3));
  };
  const res2 = await runWithConcurrency(items, throwing, 4);
  check('errors_did_not_abort', s2.processed.length === N);
  check('errors_collected', res2.errors.length === Math.ceil(N / 7));
  check('peak_within_limit_2', s2.peak <= 4);

  // Empty input resolves cleanly.
  const res3 = await runWithConcurrency([], makeWorker({active:0,peak:0,processed:[]}), 4);
  check('empty_ok', res3.completed === 0);

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_threw ' + e.message + '\n' + e.stack); });
"""


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_async_pool_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(['node', harness, js_source_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_async_pool_caps_concurrency():
    pool_js = os.path.join(JS_DIR, 'core', 'async_pool.js')
    proc = _run_harness(pool_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'async-pool failures:\n' + output
    assert output.count('PASS') >= 9, f'expected >=9 PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_async_pool_neuter_uncaps_and_fails(tmp_path):
    """NEUTER: force the cap to Infinity → all 50 workers run at once → peak
    == 50, so peak_within_limit / peak_reached_limit FAIL. Proves the cap is
    what bounds concurrency, not the harness timing."""
    pool_js = os.path.join(JS_DIR, 'core', 'async_pool.js')
    with open(pool_js, encoding='utf-8') as f:
        src = f.read()
    marker = 'var cap = (typeof limit === \'number\' && limit >= 1) ? Math.floor(limit) : 4;'
    assert marker in src, 'cap marker not found — update the neuter target'
    neutered = src.replace(marker, 'var cap = Infinity;  // NEUTER: no cap', 1)
    nfile = tmp_path / 'async_pool_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('peak_within_limit') is False, (
        'NEUTER did not bite: peak stayed within limit even with no cap — '
        'the test does not actually measure the cap.\n' + output)
    # All items are still processed even uncapped (correctness of fan-out).
    assert lines.get('all_processed') is True, output
