"""jsdom regression for the known-stale cache first-paint suppression.

WHY
---
On conversation load, Phase-1 paints from the IndexedDB cache instantly, then
Phase-2 fetches the authoritative server copy. On a poor connection the Phase-2
GET can lag seconds behind, so the user briefly SEES stale cached content as if
it were truth (the "wrong content on bad network" symptom). The server-issued
`rev` CAS already prevents this stale copy from ever being PERSISTED back over
fresh server truth — but the transient visual flash remained.

THE FIX (static/js/core/conversations.js)
------------------------------------------
The sidebar list carries each conv's server-issued `updatedAt`
(loadConversationsFromServer). If it is strictly NEWER than the cached copy's
`updatedAt`, we KNOW the cache is stale before Phase-2 returns. We still paint
instantly (no blank wait), but add the `chat-cache-verifying` class to #chatInner
so the provisional content reads as "being checked" (dimmed), and clear it the
moment Phase-2 settles. When the cache is as-fresh-or-newer, no dim is applied.

CHECKS (drive the REAL shipped conversations.js under node)
-----------------------------------------------------------
(A) STALE cache (server updatedAt > cached updatedAt): #chatInner gets
    `chat-cache-verifying` during Phase-1, and it is CLEARED after Phase-2.
(B) FRESH cache (cached updatedAt >= server updatedAt): the class is NEVER added.

NEUTER: defuse the `_cacheKnownStale` computation on a COPY (force it false) →
(A) fails: the stale paint is no longer marked verifying. Proves the gate is
load-bearing. Real file left byte-identical.
Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
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


# argv[2] = conversations.js path (a defused COPY can be swapped in).
# env STALE=1 → cached copy is older than the server list entry (should dim).
# env STALE=0 → cached copy is as fresh as the server (should NOT dim).
_HARNESS = r"""
const fs = require('fs');
global.window = global;

const STALE = process.env.STALE !== '0';
const CACHED_AT = 1700000000000;
const SERVER_AT = STALE ? 1700000005000 : 1700000000000;  // stale → server newer

// ── Track class changes on the #chatInner element. ──
let _dimAddedEver = false;
let _dimNow = false;
const classList = {
  add: (c) => { if (c === 'chat-cache-verifying') { _dimAddedEver = true; _dimNow = true; } },
  remove: (c) => { if (c === 'chat-cache-verifying') { _dimNow = false; } },
  contains: (c) => c === 'chat-cache-verifying' && _dimNow,
};
const innerEl = { classList };
global.document = { getElementById: (id) => (id === 'chatInner' ? innerEl : null) };

// ── Fake cache: returns a copy OLDER than the server list entry. ──
const cachedConv = {
  id: 'c1', title: 'cached', updatedAt: CACHED_AT, cachedAt: CACHED_AT,
  messages: [{ role: 'user', content: 'hi', timestamp: CACHED_AT }],
  settings: {},
};
global.ConvCache = {
  isAvailable: () => true,
  get: () => Promise.resolve(JSON.parse(JSON.stringify(cachedConv))),
  getMeta: () => Promise.resolve(null),
  put: () => Promise.resolve(),
  remove: () => Promise.resolve(),
};

// ── Server GET (Phase-2): authoritative, one extra message + rev. ──
global.Api = {
  conversations: {
    getResponse: async () => ({
      ok: true, status: 200,
      headers: { get: () => null },
      json: async () => ({
        messages: [
          { role: 'user', content: 'hi', timestamp: CACHED_AT },
          { role: 'assistant', content: 'server reply', timestamp: SERVER_AT },
        ],
        updatedAt: SERVER_AT, rev: 7, title: 'server',
      }),
    }),
    get: async () => ({ messages: [], updatedAt: SERVER_AT }),
    put: async () => ({ ok: true }),
  },
};

global.activeStreams = new Map();
global.activeConvId = 'c1';
global.debugLog = () => {};
global.console = console;
global.config = {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global.attachCompactionMarkersToConversation = undefined;
global.Icon = () => '';
global.AbortSignal = { timeout: () => undefined };
global.conversations = [];
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL core/conversations.js
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof loadConversationMessages !== 'function') {
  console.log('FAIL fn_exposed'); process.exit(0);
}
check('fn_exposed', true);

// Seed the conv shell in the sidebar list with the SERVER-issued updatedAt (as
// loadConversationsFromServer would have set it) — this is the staleness signal.
conversations.push({
  id: 'c1', title: 'sidebar', messages: [], _needsLoad: true,
  _serverMsgCount: 2, updatedAt: SERVER_AT, activeTaskId: null,
});

(async () => {
  await loadConversationMessages('c1');
  if (STALE) {
    // (A) stale cache → dim was applied at Phase-1 and cleared after Phase-2.
    check('stale_dim_applied', _dimAddedEver === true);
    check('stale_dim_cleared_after_phase2', _dimNow === false);
  } else {
    // (B) fresh cache → dim NEVER applied.
    check('fresh_no_dim', _dimAddedEver === false);
  }
  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str, stale: bool = True) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_stale_cache_paint_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    env = dict(os.environ)
    env['STALE'] = '1' if stale else '0'
    try:
        return subprocess.run(
            ['node', harness, js_path],
            capture_output=True, text=True, timeout=60, env=env,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_stale_cache_paint_marked_verifying_then_cleared():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js, stale=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'stale-cache dim failures:\n' + output
    assert 'PASS stale_dim_applied' in output, output
    assert 'PASS stale_dim_cleared_after_phase2' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_fresh_cache_never_dimmed():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run(conv_js, stale=False)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'fresh-cache dim failures:\n' + output
    assert 'PASS fresh_no_dim' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_stale_gate_is_load_bearing(tmp_path):
    """NEUTER: on a COPY, force `_cacheKnownStale` to false → the stale paint is
    no longer marked verifying → stale_dim_applied FAILS. Proves the staleness
    gate is what triggers the dim. Real file untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()

    needle = 'const _cacheKnownStale = (conv.updatedAt || 0) > (cached.updatedAt || 0);'
    assert src.count(needle) == 1, 'stale-gate fragment drifted — update the neuter target'
    neutered = src.replace(needle, 'const _cacheKnownStale = false;  /* neutered */', 1)
    assert neutered != src, 'neuter produced no change'

    copy = tmp_path / 'conversations_neutered.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run(str(copy), stale=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL stale_dim_applied' in output, (
        'NEUTER did not bite: stale paint was still dimmed without the gate.\n' + output
    )

    with open(conv_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped conversations.js'
