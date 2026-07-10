"""Regression (separation-of-concerns / server-authoritative): the sidebar
list-merge in `loadConversationsFromServer` (static/js/core/conversations.js)
must decide body-staleness of an already-loaded conv from the MONOTONIC
server-issued `rev` — the same signal the body loader (:1278) and the notify
push gate on — NOT the skew-prone wall-clock `updatedAt` tiebreaker.

WHY
---
The merge historically gated on `serverMsgCount > local.messages.length || sT >
mT`, where `sT`/`mT` are wall-clock `updatedAt`. Across two devices whose clocks
disagree, a stale server snapshot with a LATER wall-clock could flip
`_needsLoad=true` on the OPEN conversation → a spurious body refetch → the
"positions keep shifting" symptom — even though `rev` proves nothing changed.
Conversely a genuine same-count in-place extend on a device whose clock happens
to lag would be MISSED.

THE FIX
-------
When `rev` is comparable on BOTH sides (`sc.rev` and `local._serverRev` are
numbers) it is AUTHORITATIVE: mark stale iff `sc.rev > local._serverRev`.
Wall-clock is a FALLBACK only for a legacy rev=0 / pre-rev row. The merge also
adopts a forward-moving `sc.rev` into `local._serverRev` (CAS base advance).

This drives the REAL shipped `loadConversationsFromServer` under node.

DOUBLE-NEUTER: replace the rev-authoritative gate with the OLD wall-clock-only
referee in a COPY → a skew-later-clock stale snapshot now WRONGLY marks the open
conv stale. Real file untouched.
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
CONV_JS = os.path.join(JS_DIR, 'core', 'conversations.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

// The server list stub — set per-scenario by the test body.
let SERVER_LIST = [];

global.activeConvId = 'c1';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.conversations = [];
global.debugLog = () => {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.saveConversations = () => {};
global.loadConversationMessages = async () => {};
global.loadFolders = async () => {};
global._applySettingsToConv = () => {};
global._migratePinnedToFolder = () => {};
global._ensureMsgId = (m) => m;
let prunedIds = [];       // ConvCache.remove(id) — IDB tombstone prune
let rescuedIds = [];      // conv re-PUT rescue (observed via Api.conversations.put)
global.ConvCache = { put() {}, remove(id) { prunedIds.push(id); }, get: async () => null };
global._bootLoadInFlight = false;
global.Api = {
  conversations: {
    list: async () => SERVER_LIST,
    get: async () => null,
    // The real syncConversationToServer (declared in conversations.js, shadows
    // any pre-eval stub) issues a PUT to persist a rescued conv — capture the
    // rescue by its EFFECT here rather than fighting function-decl shadowing.
    put: async (id) => { rescuedIds.push(id); return { ok: true, rev: 1 }; },
    save: async (id) => { rescuedIds.push(id); return { ok: true, rev: 1 }; },
  },
};
// loadConversationsFromServer may fetch via Api.conversations.list OR a raw
// fetch fallback; provide both.
global.fetch = async () => ({
  ok: true, status: 200,
  headers: { get: () => null },
  json: async () => SERVER_LIST,
});
global.apiUrl = (p) => p;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/conversations.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function seedLocal(over) {
  const c = Object.assign({
    id: 'c1', title: 't', messages: [{ role: 'user' }, { role: 'assistant', content: 'a' }],
    _serverMsgCount: 2, _serverRev: 5, _needsLoad: false,
    updatedAt: 1000, createdAt: 900,
  }, over || {});
  global.conversations = [c];
  return c;
}
// A server list row (metadata shape from _conv_row_to_meta_dict).
function srvRow(over) {
  return Object.assign({
    id: 'c1', title: 't', messageCount: 2, msgCount: 2,
    updatedAt: 1000, createdAt: 900, settings: null, rev: 5,
  }, over || {});
}

const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

(async () => {
  if (typeof loadConversationsFromServer !== 'function') {
    console.log('FAIL fn_exposed loadConversationsFromServer missing'); return;
  }
  check('fn_exposed', true);

  // ══ 1. SKEW TRAP: server rev EQUAL but server wall-clock LATER → NOT stale ══
  {
    const c = seedLocal({ _serverRev: 5, updatedAt: 1000 });
    SERVER_LIST = [srvRow({ rev: 5, updatedAt: 9999 })];  // later clock, same rev
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    check('skew_equal_rev_not_stale', c._needsLoad === false);
  }

  // ══ 2. GENUINE change: server rev HIGHER (same msg count) → stale + adopt rev ══
  {
    const c = seedLocal({ _serverRev: 5, updatedAt: 1000 });
    SERVER_LIST = [srvRow({ rev: 6, updatedAt: 1000 })];  // same clock, higher rev
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    check('higher_rev_marks_stale', c._needsLoad === true);
    check('higher_rev_adopted_as_base', c._serverRev === 6);
  }

  // ══ 3. rev NEVER moves BACKWARD from a lagging list snapshot ══
  {
    const c = seedLocal({ _serverRev: 8, updatedAt: 1000 });
    SERVER_LIST = [srvRow({ rev: 6, updatedAt: 9999 })];  // lower rev, later clock
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    check('rev_not_rewound', c._serverRev === 8);
    check('lower_rev_not_stale', c._needsLoad === false);
  }

  // ══ 4. LEGACY fallback: no rev on either side → wall-clock still triggers ══
  {
    const c = seedLocal({ updatedAt: 1000 });
    delete c._serverRev;
    SERVER_LIST = [srvRow({ updatedAt: 5000 })];
    SERVER_LIST[0].rev = 0;  // pre-rev row (server sends 0)
    // With mR=null (no local rev), revComparable=false → wall-clock fallback.
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    check('legacy_wallclock_fallback_stale', c._needsLoad === true);
  }

  // ══ 5. New MESSAGE (count grew) → stale regardless of rev/clock ══
  {
    const c = seedLocal({ _serverRev: 5, updatedAt: 1000 });
    SERVER_LIST = [srvRow({ rev: 6, messageCount: 3, msgCount: 3, updatedAt: 1000 })];
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    check('count_grew_marks_stale', c._needsLoad === true);
  }

  // ══ 6. RESCUE-vs-DELETION (epic pt_2fd936cd15c34a7f) ──
  //   A conv absent from the server list that WAS server-known (had a
  //   _serverRev) = deleted elsewhere → drop + prune IDB, do NOT re-PUT.
  {
    prunedIds = []; rescuedIds = [];
    // background conv 'gone' was server-known (rev=4); active conv 'c1' present.
    global.activeConvId = 'c1';
    global.conversations = [
      { id: 'c1', title: 't', messages: [{ role: 'user' }], _serverRev: 5, _serverMsgCount: 1, updatedAt: 1000, createdAt: 900 },
      { id: 'gone', title: 'deleted-elsewhere', messages: [{ role: 'user' }, { role: 'assistant', content: 'x' }], _serverRev: 4, _serverMsgCount: 2, updatedAt: 1000, createdAt: 900 },
    ];
    SERVER_LIST = [srvRow({ id: 'c1', rev: 5, messageCount: 1, msgCount: 1 })];  // 'gone' absent
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    check('deleted_not_resurrected', !rescuedIds.includes('gone'));
    check('deleted_dropped_locally', !conversations.some(c => c.id === 'gone'));
    check('deleted_idb_pruned', prunedIds.includes('gone'));
  }

  // ══ 7. GENUINE offline-created rescue: never server-known → re-PUT ══
  {
    prunedIds = []; rescuedIds = [];
    global.activeConvId = 'c1';
    const offline = { id: 'offl', title: 'sent while offline', messages: [{ role: 'user', content: 'q' }], updatedAt: 1000, createdAt: 900 };
    // NO _serverRev on the offline conv.
    global.conversations = [
      { id: 'c1', title: 't', messages: [{ role: 'user' }], _serverRev: 5, _serverMsgCount: 1, updatedAt: 1000, createdAt: 900 },
      offline,
    ];
    SERVER_LIST = [srvRow({ id: 'c1', rev: 5, messageCount: 1, msgCount: 1 })];  // 'offl' absent
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    /* The genuine offline conv takes the RESCUE branch, not the deletion drop:
     *   it is NEITHER pruned from IDB NOR removed from the list (it stays so the
     *   re-PUT can persist it). We assert the observable SURVIVAL outcome — the
     *   re-PUT itself is the pre-existing, unchanged syncConversationToServer
     *   path (not this epic's change), so we don't re-simulate its full body. */
    check('offline_not_pruned', !prunedIds.includes('offl'));
    check('offline_kept_locally', conversations.some(c => c.id === 'offl'));
    check('offline_not_dropped_as_deletion', conversations.some(c => c.id === 'offl'));
  }

  console.log(out.join('\n'));
})();
"""


def _run(js_path: str):
    harness = os.path.join(HERE, '_list_merge_rev_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(['node', harness, js_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_list_merge_rev_authority():
    proc = _run(CONV_JS)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'list-merge rev-authority regressions:\n' + output
    for inv in ('skew_equal_rev_not_stale', 'higher_rev_marks_stale',
                'higher_rev_adopted_as_base', 'rev_not_rewound',
                'lower_rev_not_stale', 'legacy_wallclock_fallback_stale',
                'count_grew_marks_stale',
                'deleted_not_resurrected', 'deleted_dropped_locally',
                'deleted_idb_pruned', 'offline_not_pruned',
                'offline_kept_locally', 'offline_not_dropped_as_deletion'):
        assert f'PASS {inv}' in output, f'expected {inv} to PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_list_merge_rev_authority_neuter(tmp_path):
    """NEUTER: revert to the wall-clock-only referee (drop the rev gate) in a
    COPY → the skew trap (equal rev, later clock) now WRONGLY marks the open
    conv stale, proving the rev-authority gate is load-bearing. Real file
    untouched."""
    with open(CONV_JS, encoding='utf-8') as f:
        src = f.read()
    anchor = 'if (serverMsgCount > local.messages.length || revNewer || sT > mT) {'
    assert anchor in src, 'rev-gate anchor not found — update the neuter target'
    neutered = src.replace(
        anchor,
        'if (serverMsgCount > local.messages.length || sT > mT) {', 1)
    # Also neuter the inner content-stale decision back to wall-clock only.
    inner = 'const _contentStale = revComparable ? revNewer : (sT > mT);'
    assert inner in src, 'inner content-stale anchor not found'
    neutered = neutered.replace(inner, 'const _contentStale = (sT > mT);', 1)
    assert neutered != src, 'neuter did not change source'
    nfile = tmp_path / 'conversations_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')

    proc = _run(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('skew_equal_rev_not_stale') is False, (
        'NEUTER did not bite: wall-clock-only referee did NOT wrongly mark the '
        'skew case stale — the test does not discriminate the fix.\n' + output)

    with open(CONV_JS, encoding='utf-8') as f:
        assert f.read() == src, 'shipped conversations.js was mutated by the neuter test'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_rescue_deletion_disambiguator_neuter(tmp_path):
    """NEUTER: force the rescue-vs-deletion disambiguator to treat EVERY absent
    conv as an offline rescue (the OLD blanket behaviour) in a COPY → a
    server-known-but-absent conv (deleted elsewhere) is WRONGLY re-PUT
    (resurrected) instead of dropped. Proves the `_serverRev` disambiguator is
    load-bearing. Real file untouched."""
    with open(CONV_JS, encoding='utf-8') as f:
        src = f.read()
    anchor = "const wasServerKnown = (typeof lc._serverRev === 'number');"
    assert anchor in src, 'disambiguator anchor not found — update the neuter target'
    neutered = src.replace(anchor, 'const wasServerKnown = false;', 1)
    assert neutered != src, 'neuter did not change source'
    nfile = tmp_path / 'conversations_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')

    proc = _run(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # With wasServerKnown pinned false, the deleted conv takes the RESCUE path
    # (not the deletion drop) → it is no longer pruned from IDB. That observable
    # flip proves the `_serverRev` disambiguator is load-bearing.
    assert lines.get('deleted_idb_pruned') is False, (
        'NEUTER did not bite: the server-known-but-absent (deleted) conv was '
        'still pruned even with the disambiguator disabled — the test does not '
        'discriminate the fix.\n' + output)

    with open(CONV_JS, encoding='utf-8') as f:
        assert f.read() == src, 'shipped conversations.js was mutated by the neuter test'
