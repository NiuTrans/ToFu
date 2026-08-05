"""Guard: per-conv in-flight merge on Api.conversations.get (pt_afbaf3d7 ③,
hand-off acceptance from pt_ef42c2a1e9f946f3).

The 2026-08-01 hard-refresh congestion collapse served the SAME 176.8 MB
conversation 6× in 25s — boot load, notify verify, push-reconnect catch-up
and Case-F recovery each issued their OWN full GET. The fix: identical
in-flight GETs share ONE Promise.

ACCEPTANCE INVARIANT (the hand-off's stated gate): concurrent same-shape
GETs for the same conv ≤ 1 on the wire.

Checks (drive the REAL shipped api.js under node):
  A. Two concurrent signal-less gets, same conv       → fetch ×1, same result.
  B. Two concurrent gets, different convs             → fetch ×2.
  C. A SIGNALLED get bypasses the merge               → fetch ×2 (its abort
     budget must never cancel a shared read).
  D. Different windowing shape (?window=3 vs full)    → keyed apart, fetch ×2.
  E. After the in-flight read SETTLES, a new get      → fetch again (map cleared).
  F. A rejected in-flight read still clears the map   → next call re-fetches.

NEUTER: drop the `if (hit) return hit;` merge on a COPY → (A) fails (fetch ×2),
(B)–(F) stay green — the check discriminates the merge, not the file.
Skips cleanly when node isn't installed.

Also pins the sibling half of ③: the `_recoverOfflineConversations` worker
in cross_tab_sync.js must fetch the TAIL WINDOW (?window=3), not the full
blob — the thundering-herd source after an overnight sleep.
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
global.location = { pathname: '/', protocol: 'http:', host: 'localhost' };

// ── Controllable fetch: counts calls, resolves a per-call JSON response. ──
let _fetchCalls = 0;
let _failNext = false;
global.fetch = (url, init) => {
  _fetchCalls++;
  const shouldFail = _failNext;
  _failNext = false;
  const body = shouldFail ? 'boom' : JSON.stringify({ id: url, messages: [] });
  const resp = {
    ok: !shouldFail,
    status: shouldFail ? 500 : 200,
    headers: { get: () => 'application/json' },
    text: () => Promise.resolve(body),
    json: () => Promise.resolve(JSON.parse(body)),
  };
  return new Promise((resolve) => setTimeout(() => resolve(resp), 5));
};

eval(fs.readFileSync(process.argv[2], 'utf8'));   // REAL api.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  const get = global.Api.conversations.get;

  // (A) two concurrent signal-less gets for the SAME conv → ONE fetch.
  const [a1, a2] = await Promise.all([get('conv-a'), get('conv-a')]);
  check('A_same_conv_one_fetch', _fetchCalls === 1);
  check('A_same_result', a1 && a2 && a1.id === a2.id);

  // (B) different convs are NOT merged.
  _fetchCalls = 0;
  await Promise.all([get('conv-a'), get('conv-b')]);
  check('B_diff_conv_two_fetches', _fetchCalls === 2);

  // (C) a signalled caller bypasses the merge (its abort budget is its own).
  _fetchCalls = 0;
  const sig = (typeof AbortController !== 'undefined') ? new AbortController().signal : undefined;
  await Promise.all([get('conv-c'), get('conv-c', { signal: sig })]);
  check('C_signalled_bypasses_merge', _fetchCalls === 2);

  // (D) different windowing shape is keyed apart (full vs tail-window).
  _fetchCalls = 0;
  await Promise.all([get('conv-d'), get('conv-d', { query: { window: '3' } })]);
  check('D_window_shape_keyed_apart', _fetchCalls === 2);

  // (E) after settle, a fresh get re-fetches (in-flight entry cleared).
  _fetchCalls = 0;
  await get('conv-e');
  await get('conv-e');
  check('E_settled_refetches', _fetchCalls === 2);

  // (F) a rejected in-flight read clears the map (onError:'null' resolves
  //     null, so simulate a transport-level failure instead: fetch throws).
  _fetchCalls = 0;
  const realFetch = global.fetch;
  global.fetch = () => Promise.reject(new Error('network down'));
  const f1 = await get('conv-f');             // resolves null via onError:'null'
  global.fetch = realFetch;
  const f2 = await get('conv-f');             // must RE-FETCH, not reuse a dead entry
  check('F_failed_read_clears_map', f1 === null && f2 && f2.id !== undefined && _fetchCalls === 1);

  console.log(out.join('\n'));
})().catch((e) => { console.log('HARNESS-ERROR ' + (e && e.stack || e)); });
"""


def _run_harness(api_js_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_conv_get_dedup_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, api_js_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_conv_get_inflight_dedup():
    api_js = os.path.join(JS_DIR, 'api.js')
    proc = _run_harness(api_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'HARNESS-ERROR' not in output, output
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'conversations.get dedup failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_conv_get_dedup_neuter(tmp_path):
    """NEUTER: drop the merge hit on a COPY of api.js → (A) fails with TWO
    fetches for the same conv, every other check stays green. Proves (A)
    discriminates the dedup itself. Shipped file left byte-identical."""
    api_js = os.path.join(JS_DIR, 'api.js')
    with open(api_js, encoding='utf-8') as f:
        src = f.read()

    needle = "    const hit = _convGetInflight.get(key);\n    if (hit) return hit;\n"
    assert needle in src, 'dedup-hit fragment drifted — update the neuter target'
    neutered = src.replace(needle, "    const hit = null;\n", 1)

    copy = tmp_path / 'api_no_dedup.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run_harness(str(copy))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert fails == ['FAIL A_same_conv_one_fetch'], (
        'NEUTER should bite EXACTLY the same-conv single-fetch check:\n' + output)

    with open(api_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped api.js'


def _recover_worker_span(src: str) -> str:
    """The `_recoverWorker` body span (start marker → its GET adoption block)."""
    start = src.index('_recoverWorker = async (conv) =>')
    end = src.index('// ★ Only persist and count as recovered', start)
    return src[start:end]


def test_recover_worker_fetches_tail_window_not_full_blob():
    """The offline-recovery worker inspects ONLY the trailing message, so its
    GET must be the windowed tail read (?window=3 — O(3) rows from the
    normalized store), not the full blob that made one 176.8 MB conv get
    served 6× in 25s. NEUTER (revert the query on a copy) → predicate False."""
    with open(os.path.join(JS_DIR, 'core', 'cross_tab_sync.js'), encoding='utf-8') as f:
        src = f.read()
    span = _recover_worker_span(src)
    assert "query: { window: '3' }" in span, (
        'recover worker lost its windowed tail read — the full-blob '
        'thundering herd is back')
    # The full-GET shape (no query at all) must not coexist in the worker.
    assert 'Api.conversations.get(conv.id, { signal: AbortSignal.timeout(10000) })' not in span

    # NEUTER: strip the window param on a copy → the pin predicate flips.
    neutered = span.replace("query: { window: '3' },", '', 1)
    assert "query: { window: '3' }" not in neutered
