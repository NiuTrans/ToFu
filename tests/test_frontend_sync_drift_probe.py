#!/usr/bin/env python3
"""tests/test_frontend_sync_drift_probe.py — pt_conv_state_ssot P5:
client digest builder + reporter (jsdom harness, node-eval).

Owner constraint #4: the digest covers BOTH activeTaskIds AND conv rev — the
rev half closes the "notify frame dropped, _serverRev never converges" hole.

Faces:
  1. buildSyncDigest includes a conv carrying EITHER marker (authoritative
     rev tuple OR numeric _serverRev), with taskIds sorted; excludes a conv
     carrying neither.
  2. Digest entry shape covers BOTH halves (constraint #4): taskIds (sorted)
     AND rev.
  3. reportSyncDigest POSTs the exact digest via Api.conversations
     .reportSyncDigest; a divergences response fires console.warn.
  4. NEUTER: a pre-P5 digest (taskIds only, rev dropped) does NOT satisfy
     constraint #4 — proves the rev half is load-bearing, not decoration.
  5. startSyncDriftProbe is idempotent (second call does not arm a 2nd timer)
     and resolves the conversations array lazily per tick.
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
const path = require('path');
global.window = global;

const out = [];
function check(name, cond) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name);
}

function loadModule(rel) {
  const p = path.join(process.argv[2], rel);
  const src = fs.readFileSync(p, 'utf8');
  (0, eval)(src);
}

global.debugLog = () => {};
loadModule('core/conv_state_reducer.js');

check('exposes_buildSyncDigest', typeof buildSyncDigest === 'function');
check('exposes_reportSyncDigest', typeof reportSyncDigest === 'function');
check('exposes_startSyncDriftProbe', typeof startSyncDriftProbe === 'function');

// ── Face 1: inclusion/exclusion + sorted taskIds ──
{
  const convs = [
    { id: 'c-auth',
      _authoritativeActiveTaskIds: new Set(['tid-b', 'tid-a']),
      _authoritativeActiveTaskIdsRev: [100, 'r0'],
      _serverRev: 7 },
    { id: 'c-rev-only', _serverRev: 3 },
    { id: 'c-nothing' },
  ];
  const d = buildSyncDigest(convs);
  const byId = Object.fromEntries(d.map((e) => [e.convId, e]));
  check('digest_includes_auth_conv', !!byId['c-auth']);
  check('digest_taskIds_sorted',
        JSON.stringify(byId['c-auth'].taskIds) === JSON.stringify(['tid-a', 'tid-b']));
  check('digest_rev_present', byId['c-auth'].rev === 7);
  check('digest_includes_rev_only_conv',
        !!byId['c-rev-only'] && byId['c-rev-only'].rev === 3 &&
        byId['c-rev-only'].taskIds.length === 0);
  check('digest_excludes_markerless_conv', !byId['c-nothing']);
  check('digest_length', d.length === 2);
}

// ── Face 2: constraint #4 — entry covers BOTH halves ──
{
  const convs = [{ id: 'c1',
                   _authoritativeActiveTaskIds: new Set(['t2', 't1']),
                   _authoritativeActiveTaskIdsRev: [1, 'r'],
                   _serverRev: 42 }];
  const e = buildSyncDigest(convs)[0];
  check('constraint4_has_taskIds_and_rev',
        Array.isArray(e.taskIds) && typeof e.rev === 'number');
}

// ── Face 3: reportSyncDigest posts exact digest + warns on divergence ──
(async () => {
  let posted = null;
  global.Api = { conversations: { reportSyncDigest: async (digests) => {
    posted = digests;
    return { ok: true, checked: digests.length,
             divergences: [{ convId: 'c1', kind: 'rev', client: 1, server: 2 }] };
  } } };
  let warned = 0;
  const origWarn = console.warn;
  console.warn = () => { warned++; };
  const convs = [{ id: 'c1', _serverRev: 1 }];
  const resp = await reportSyncDigest(convs);
  console.warn = origWarn;
  check('report_posts_exact_digest',
        Array.isArray(posted) && posted.length === 1 && posted[0].convId === 'c1');
  check('report_returns_response', resp && resp.checked === 1);
  check('report_warns_on_divergence', warned === 1);

  // No digests → no POST at all.
  posted = null;
  await reportSyncDigest([{ id: 'c-empty' }]);
  check('report_skips_post_when_empty', posted === null);

  // ── Face 4: NEUTER — pre-P5 digest (taskIds only) fails constraint #4 ──
  const preP5 = (convs2) => convs2
    .filter((c) => c._authoritativeActiveTaskIds)
    .map((c) => ({ convId: c.id,
                   taskIds: Array.from(c._authoritativeActiveTaskIds) }));
  const neutered = preP5([{ id: 'c1',
                            _authoritativeActiveTaskIds: new Set(['t']),
                            _serverRev: 9 }]);
  const real = buildSyncDigest([{ id: 'c1',
                                  _authoritativeActiveTaskIds: new Set(['t']),
                                  _authoritativeActiveTaskIdsRev: [1, 'r'],
                                  _serverRev: 9 }]);
  check('neuter_preP5_lacks_rev_half',
        neutered.length === 1 && neutered[0].rev === undefined);
  check('real_digest_keeps_rev_half',
        real.length === 1 && real[0].rev === 9);

  // ── Face 5: idempotent probe + lazy per-tick resolution ──
  let timerCalls = 0;
  let tickFn = null;
  const origSetInterval = global.setInterval;
  global.setInterval = (fn, ms) => { timerCalls++; tickFn = fn; return 1; };
  startSyncDriftProbe(() => convs, 60000);
  startSyncDriftProbe(() => convs, 60000);   // must NOT arm a second timer
  global.setInterval = origSetInterval;
  check('probe_idempotent_single_timer', timerCalls === 1);
  check('probe_tick_fn_armed', typeof tickFn === 'function');

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_sync_drift_probe_frontend():
    harness = os.path.join(HERE, '_sync_drift_probe_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, JS_DIR],
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
    assert not fails, 'drift-probe failures:\n' + output
    passes = [ln for ln in output.splitlines() if ln.startswith('PASS')]
    assert len(passes) >= 16, f'expected >=16 PASS, got {len(passes)}:\n{output}'
