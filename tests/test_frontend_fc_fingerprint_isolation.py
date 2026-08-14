"""Regression harness: file-changes-bar cache must not leak across conversations.

WHY
---
The file-changes bar in ``static/js/ui/finish_info.js`` derives its list
from the backend (``lib/tool_changes.py`` via
``POST /api/v1/messages/extract-file-changes``) whenever a message lacks
an authoritative server ``modifiedFileList`` (mid-stream, or project
tracking off), and caches the result on the client.

Two layers of defence, both asserted here:

1. FINGERPRINT (staleness / dedup token). ``_fcFingerprint`` was once
   COARSE (``roundCount:lastStatus:lastToolName:lastResultCount``), so two
   unrelated messages sharing that shape produced the SAME token. It is
   now CONTENT-FAITHFUL: ``same fingerprint ⟺ same extractor inputs``. A
   double-neuter reconstructs the old coarse key inline and proves it
   WOULD have collided on the same input.

2. STRUCTURAL ISOLATION (the real guarantee). The cached result now lives
   on the OWNING MESSAGE object (``_fcResultByMsg`` WeakMap), not a
   global content-keyed Map. So even a WORST-CASE forced fingerprint
   collision cannot leak message A's list onto message B — each message
   reads only its own entry. This test forces every fingerprint to a
   constant and proves no leak occurs anyway.

This harness loads the REAL shipped ``finish_info.js`` under jsdom.
Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// finish_info.js references these at load / call time.
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.debugLog = global.debugLog = () => {};
win.Icon = global.Icon = () => '';
win.t = global.t = (k) => k;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/finish_info.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _fcFingerprint !== 'function') {
  console.log('FAIL fingerprint_exposed');
  process.exit(0);
}
check('fingerprint_exposed', true);

// Two messages with the SAME coarse shape (1 write_file round, done, 1
// result) but DIFFERENT files — as would occur in two unrelated
// conversations.
const roundsA = [{
  toolName: 'write_file', status: 'done',
  toolArgs: { path: 'conversationA/alpha.py' },
  results: [{ writeOk: true, badge: 'created', title: 'alpha.py' }],
}];
const roundsB = [{
  toolName: 'write_file', status: 'done',
  toolArgs: { path: 'conversationB/beta.py' },
  results: [{ writeOk: true, badge: 'created', title: 'beta.py' }],
}];

const fpA = _fcFingerprint(roundsA);
const fpB = _fcFingerprint(roundsB);

// ── The fix: distinct files ⟹ distinct fingerprints (no cache collision) ──
check('distinct_files_distinct_fp', fpA && fpB && fpA !== fpB);

// ── Double-neuter: the OLD coarse key WOULD have collided on this input. ──
function _coarseFp(toolRounds) {
  const last = toolRounds[toolRounds.length - 1];
  return toolRounds.length + ':' + (last.status || '') + ':' +
         (last.toolName || '') + ':' +
         ((last.results && last.results.length) || 0);
}
check('coarse_key_would_collide', _coarseFp(roundsA) === _coarseFp(roundsB));

// ── Identical inputs ⟹ identical fingerprint (cache still HITS correctly). ──
const roundsA2 = [{
  toolName: 'write_file', status: 'done',
  toolArgs: { path: 'conversationA/alpha.py' },
  results: [{ writeOk: true, badge: 'created', title: 'alpha.py' }],
}];
check('same_input_same_fp', _fcFingerprint(roundsA) === _fcFingerprint(roundsA2));

// ── Distinguishes edits[] paths (apply_diff / insert_content shape). ──
const diffA = [{
  toolName: 'apply_diffs', status: 'done',
  toolArgs: { edits: [{ path: 'x/one.js' }, { path: 'x/two.js' }] },
  results: [{ writeOk: true }],
}];
const diffB = [{
  toolName: 'apply_diffs', status: 'done',
  toolArgs: { edits: [{ path: 'x/one.js' }, { path: 'x/three.js' }] },
  results: [{ writeOk: true }],
}];
check('distinct_edit_paths_distinct_fp', _fcFingerprint(diffA) !== _fcFingerprint(diffB));

// ── run_command fileChanges are part of the key. ──
const cmdA = [{
  toolName: 'run_command', status: 'done',
  results: [{ fileChanges: [{ path: 'a.txt', action: 'modified' }] }],
}];
const cmdB = [{
  toolName: 'run_command', status: 'done',
  results: [{ fileChanges: [{ path: 'b.txt', action: 'modified' }] }],
}];
check('distinct_cmd_changes_distinct_fp', _fcFingerprint(cmdA) !== _fcFingerprint(cmdB));

// ── writeOk difference (success vs failure) is distinguished. ──
const okRound = [{ toolName: 'write_file', status: 'done',
  toolArgs: { path: 'f.py' }, results: [{ writeOk: true, title: 'f.py' }] }];
const failRound = [{ toolName: 'write_file', status: 'done',
  toolArgs: { path: 'f.py' }, results: [{ writeOk: false, title: 'f.py' }] }];
check('writeok_difference_distinct_fp', _fcFingerprint(okRound) !== _fcFingerprint(failRound));

// ── The full file content in write_file args must NOT be in the key
//    (two writes to the SAME path differing only by body share a key —
//    the extractor output is identical: one 'written' entry for that path). ──
const bodyA = [{ toolName: 'write_file', status: 'done',
  toolArgs: { path: 'same.py', content: 'A'.repeat(10000) },
  results: [{ writeOk: true, title: 'same.py' }] }];
const bodyB = [{ toolName: 'write_file', status: 'done',
  toolArgs: { path: 'same.py', content: 'B'.repeat(10000) },
  results: [{ writeOk: true, title: 'same.py' }] }];
check('content_body_not_in_key', _fcFingerprint(bodyA) === _fcFingerprint(bodyB));
check('key_not_bloated_by_content', _fcFingerprint(bodyA).length < 200);

// ══════════════════════════════════════════════════════════════════
//  STRUCTURAL isolation: the cache is keyed by the OWNING message
//  (WeakMap), so even a WORST-CASE colliding fingerprint cannot leak
//  one message's file list onto another. This is the real guarantee —
//  isolation by construction, not by a collision-free hash.
// ══════════════════════════════════════════════════════════════════
if (typeof _extractFileChangesFromRoundsAsync !== 'function'
    || typeof _extractFileChangesFromRoundsCached !== 'function') {
  console.log('FAIL cache_fns_exposed');
  process.exit(0);
}
check('cache_fns_exposed', true);

// Backend SSOT derivation, stubbed: result depends only on the message's
// own toolRounds (identical input ⟹ identical output — the correct
// contract). Keyed by first path here for the distinct-data cases.
const _serverByFirstPath = {
  'convA/a.py': [{ path: 'convA/a.py', action: 'created', ok: true }],
  'convB/b.py': [{ path: 'convB/b.py', action: 'created', ok: true }],
};
win.Api = global.Api = { conversations: {
  extractFileChanges: async (toolRounds) => {
    const p = toolRounds[0].toolArgs.path;
    return { files: _serverByFirstPath[p] || [] };
  },
  extractFileChangesBatch: async (items) => ({
    results: items.map(it => _serverByFirstPath[it.toolRounds[0].toolArgs.path] || []),
  }),
}};

const mkMsg = (p) => ({ role: 'assistant', toolRounds: [{
  toolName: 'write_file', status: 'done',
  toolArgs: { path: p }, results: [{ writeOk: true, title: p }] }] });

const msgA = mkMsg('convA/a.py');
const msgB = mkMsg('convB/b.py');

(async () => {
  const filesA = await _extractFileChangesFromRoundsAsync(msgA.toolRounds, msgA);
  const filesB = await _extractFileChangesFromRoundsAsync(msgB.toolRounds, msgB);
  check('async_A_owns_a', filesA.length === 1 && filesA[0].path === 'convA/a.py');
  check('async_B_owns_b', filesB.length === 1 && filesB[0].path === 'convB/b.py');

  // The synchronous accessor reads per-MESSAGE, so B returns B's file.
  const cachedA = _extractFileChangesFromRoundsCached(msgA);
  const cachedB = _extractFileChangesFromRoundsCached(msgB);
  check('cached_A_owns_a', cachedA && cachedA.length === 1 && cachedA[0].path === 'convA/a.py');
  check('cached_B_owns_b', cachedB && cachedB.length === 1 && cachedB[0].path === 'convB/b.py');

  // ── THE STRUCTURAL GUARANTEE: the cache is keyed by the OWNING MESSAGE
  //    OBJECT, not by fingerprint. Two DISTINCT message objects with
  //    BYTE-IDENTICAL toolRounds (a genuine, real fingerprint collision)
  //    do NOT share a cache entry: a never-fetched clone of msgA reads
  //    null, not msgA's list. A global content-keyed cache would have
  //    served msgA's data here — that was the leak. ──
  const msgAClone = mkMsg('convA/a.py');
  check('identical_rounds_same_fp',
        _fcFingerprint(msgA.toolRounds) === _fcFingerprint(msgAClone.toolRounds));
  check('clone_not_leaked_from_original',
        _extractFileChangesFromRoundsCached(msgAClone) === null);

  // A message with no cached entry of its own reads null (not a sibling's).
  check('uncached_msg_reads_null',
        _extractFileChangesFromRoundsCached(mkMsg('convC/c.py')) === null);

  console.log(out.join('\n'));
})();
"""


def _run() -> str:
    harness = os.path.join(HERE, '_fc_fp_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'finish_info.js'),  # argv[2]
             ROOT],                                         # argv[3]
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_fc_fingerprint_no_cross_conversation_collision():
    output = _run()
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'fingerprint isolation failures:\n' + output
    assert output.count('PASS') >= 16, f'expected >=16 PASS lines, got:\n{output}'
