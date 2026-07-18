"""Regression: a reconnect `state` snapshot (or a racey `done` committedMessage)
must NOT COLLAPSE an in-progress message's tool-round panel by overwriting the
accumulated rounds with an EMPTY/SHORTER array.

WHY
---
`content`/`thinking` already go through `_snapshotLonger` (keep-the-longer), but
`toolRounds` used to be assigned VERBATIM at all state-snapshot sites:

    assistantMsg.toolRounds = existing.concat(ev.toolRounds || []);   // state

On a flaky network the reconnect snapshot can replay a COLD/empty 5s checkpoint
whose `toolRounds` is `[]` (or shorter than what the client already accumulated
from live deltas) — verbatim assignment then BLANKS the searches / file-edits
panel the user was watching, mid-stream. Same content-regression class as the
2026-07-03 `_snapshotLonger` text fix, just never extended to rounds.

The fix adds `_snapshotLongerRounds(current, incoming)` — a rounds snapshot may
only GROW (adopt incoming only when `incoming.length >= current.length`) —
applied at the FOUR STATE-SNAPSHOT sites (2 planner, worker, plain-assistant
merge). These replay a mid-stream CHECKPOINT that can be cold/shorter.

The terminal `done` committedMessage site is DIFFERENT and DELIBERATELY NOT
keep-longer (epic pt_78579f57be1c4f60, 2026-07-08): once the backend stamps
`task['_committedMsg']` (manager.py, on CAS success) the done event ships the
EXACT committed DB record, so the settled bubble PROJECTS IT VERBATIM (the
separation-of-concerns directive: settled = verbatim backend record, not a
local keep-longer reconstruction). committedMessage is ABSENT on skip/crash
paths → the client keeps its transient buffer (offline fallback). So the `done`
site is verbatim `= _cm.toolRounds`, NOT routed through the keep-longer helper.

Tests (drive the REAL shipped `_snapshotLongerRounds` under node):
  1. empty incoming → keeps the current (longer) rounds. ★ THE FIX.
  2. shorter incoming → keeps current.
  3. longer incoming → adopts it (behaviour preservation).
  4. equal length → adopts incoming (idempotent, >= comparison).
  5. empty current + non-empty incoming → adopts incoming (fresh fill).
  6. null-safe.
  ★ Double-neuter: revert the helper body to `return inc;` (raw adopt) → test #1
    and #2 FAIL (panel collapsed). Plus source guards asserting the six call
    sites route through the helper, not a raw `= ev.toolRounds` / `= _cm.toolRounds`.
"""

from __future__ import annotations

import os
import re
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
const SSE = process.argv[2];

const src = fs.readFileSync(SSE, 'utf8');
const m = src.match(/function _snapshotLongerRounds\([\s\S]*?\n}/);
if (!m) { console.log('FAIL helper_not_found'); process.exit(0); }
eval(m[0]);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('helper_defined', typeof _snapshotLongerRounds === 'function');

const R = n => Array.from({length: n}, (_, i) => ({tool: 'r' + i}));

// 1. Empty incoming must NOT shrink a non-empty rounds array. ★ THE FIX.
{
  const cur = R(3), inc = [];
  check('empty_incoming_keeps_current', _snapshotLongerRounds(cur, inc) === cur);
}
// 2. Shorter incoming must NOT shrink.
{
  const cur = R(4), inc = R(1);
  check('shorter_incoming_keeps_current', _snapshotLongerRounds(cur, inc) === cur);
}
// 3. Longer incoming IS adopted.
{
  const cur = R(1), inc = R(5);
  check('longer_incoming_adopted', _snapshotLongerRounds(cur, inc) === inc);
}
// 4. Equal length → adopt incoming (idempotent).
{
  const cur = R(2), inc = R(2);
  check('equal_len_adopts_incoming', _snapshotLongerRounds(cur, inc) === inc);
}
// 5. Empty current + non-empty incoming → adopt incoming (fresh fill).
{
  const inc = R(2);
  check('empty_current_adopts_incoming', _snapshotLongerRounds([], inc) === inc);
}
// 6. null-safe (returns an array both ways).
{
  check('null_safe',
    Array.isArray(_snapshotLongerRounds(null, null))
    && _snapshotLongerRounds(null, R(2)).length === 2
    && _snapshotLongerRounds(R(2), null).length === 2);
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_toolrounds_keep_longer_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'ui', 'sse_pipeline.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'toolRounds keep-longer failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'

    # ── Source guards ──
    with open(os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'), encoding='utf-8') as f:
        sse_src = f.read()

    assert 'function _snapshotLongerRounds' in sse_src, '_snapshotLongerRounds helper removed'
    assert 'inc.length >= cur.length' in sse_src, (
        'fix regression: _snapshotLongerRounds no longer keeps-the-longer — a '
        'shorter/empty snapshot could collapse the tool-round panel.')

    # FOUR STATE-SNAPSHOT call sites route through the helper (2 planner +
    # worker + plain-assistant merge). The endpoint-critic snapshot block has no
    # toolRounds assignment; the terminal `done` site is now a VERBATIM
    # committedMessage projection (epic pt_78579f57be1c4f60), NOT keep-longer.
    # +1 for the definition = 5 occurrences.
    occurrences = len(re.findall(r'_snapshotLongerRounds\(', sse_src))
    assert occurrences >= 5, (
        f'expected >=4 _snapshotLongerRounds state-snapshot call sites (+1 def '
        f'= 5 occurrences), found {occurrences} — a STATE-SNAPSHOT toolRounds '
        'site may have regressed to a raw `= ev.toolRounds` overwrite.')

    # The specific old-shape raw STATE-SNAPSHOT assignments must be gone. The
    # `done` site is intentionally verbatim (`= _cm.toolRounds`) now, so it is
    # NOT in this banned list.
    for bad in ('plannerMsg.toolRounds = ev.toolRounds',
                'workerMsg.toolRounds = ev.toolRounds',
                'assistantMsg.toolRounds = existing.concat(ev.toolRounds || [])'):
        assert bad not in sse_src, (
            f'fix regression: raw toolRounds overwrite reintroduced: {bad!r}')

    # The `done` committedMessage projection MUST be verbatim (backend record is
    # authoritative + complete once _committedMsg is stamped) — guard it so a
    # future edit doesn't silently re-route it back through keep-longer.
    assert 'if (Array.isArray(_cm.toolRounds)) assistantMsg.toolRounds = _cm.toolRounds;' in sse_src, (
        'done-site committedMessage toolRounds must be projected VERBATIM '
        '(epic pt_78579f57be1c4f60) — not routed through _snapshotLongerRounds.')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_toolrounds_keep_longer():
    _run()


if __name__ == '__main__':
    if not _node_available():
        print('SKIP — node not available')
    else:
        _run()
        print('PASS test_toolrounds_keep_longer')
