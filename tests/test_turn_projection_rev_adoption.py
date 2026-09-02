"""Guard: applyTurnStateProjection must adopt the authoritative rev on EVERY dispatch.

THE DEFECT (measured in production, 2026-08-19)
-----------------------------------------------
A V2 conversation streaming on the server kept climbing
``conversations.rev`` (checkpoints), while the client's ``_serverRev`` stayed
frozen (5742 vs 13639) for 6.6 hours and 398 consecutive 60s digest reports.

``applyTurnStateProjection`` owns ``conversation._serverRev`` for V2
turn-protocol conversations, but it only wrote that field INSIDE the
fingerprint-changed branch.  A checkpoint (or a snapshot) that advances
``conversationRevision`` WITHOUT changing any turn's ``projectionRevision`` or
``status`` therefore produced an unchanged fingerprint and returned early —
the freshly-fetched authoritative rev was silently dropped, so the client kept
reporting the frozen value and the sync-digest repair loop never converged.

THE FIX
-------
Adopt ``state.conversationRevision`` into ``conversation._serverRev``
monotonically on EVERY projection, before the fingerprint early-return.  A
reordered/stale frame must never lower it.

Run: python3 tests/test_turn_projection_rev_adoption.py
  or PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_turn_projection_rev_adoption.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURN_PROJECTION_TS = os.path.join(
    ROOT, 'frontend', 'src', 'core', 'turn-projection.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')

_HARNESS = r"""
const fs = require('fs');
globalThis.window = globalThis;
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));

const state = {
  conversationRevision: 13639,
  transport: 'connected',
  turnsById: {
    t1: {
      turnId: 't1', actor: 'assistant', kind: 'reply', laneId: 'main',
      parentTurnId: null, status: 'completed', currentAttemptId: null,
      projectionRevision: 3, projection: { content: 'answer' },
      settlement: {}, createdAt: 1,
    },
  },
  laneOrder: { main: ['t1'] },
  attemptsById: {},
  commandPending: {},
  livePhase: null,
};

const conversation = {
  id: 'conv-turn-native',
  _serverRev: 5742, _turnProjectionFingerprint: undefined,
};

const firstChanged = applyTurnStateProjection({ conversation, state });
const firstRev = conversation._serverRev;

// Same turn projection, HIGHER rev only — the exact "checkpoint suppressed"
// shape.  The fingerprint is unchanged, but the authoritative rev moved.
state.conversationRevision = 14000;
const secondChanged = applyTurnStateProjection({ conversation, state });
const secondRev = conversation._serverRev;

// A stale/reordered frame must never regress the adopted rev.
state.conversationRevision = 12000;
applyTurnStateProjection({ conversation, state });
const thirdRev = conversation._serverRev;

// A Turn change invalidates shell consumers without materializing content.
state.conversationRevision = 14001;
state.turnsById.t1 = {
  ...state.turnsById.t1,
  projectionRevision: 4,
  projection: { content: 'new answer' },
};
const fourthChanged = applyTurnStateProjection({ conversation, state });

// Heartbeat sequence is an invalidation token, not a retry attempt.  The HUD
// must repaint elapsed/current-attempt status even when phase/detail are stable.
state.livePhase = {
  phase: 'waiting_model', detailKey: 'stream.phase.waitingForResponse', seq: 40,
};
const firstPhaseChanged = applyTurnStateProjection({ conversation, state });
state.livePhase = { ...state.livePhase, seq: 41 };
const heartbeatChanged = applyTurnStateProjection({ conversation, state });
const duplicateHeartbeatChanged = applyTurnStateProjection({ conversation, state });

console.log(JSON.stringify({
  firstChanged, firstRev, secondChanged, secondRev, thirdRev,
  needsLoad: conversation._turnSnapshotRequired,
  fourthChanged,
  firstPhaseChanged, heartbeatChanged, duplicateHeartbeatChanged,
  serverMsgCount:conversation._serverTurnCount,
  transport:conversation._turnTransport,
  hasTranscript:Object.prototype.hasOwnProperty.call(conversation, 'messages'),
  hasPhase:Object.prototype.hasOwnProperty.call(conversation, 'livePhase'),
}));
"""


@pytest.mark.skipif(not shutil.which('node') or not os.path.isfile(ESBUILD),
                    reason='node/esbuild dev-deps not installed')
def test_rev_only_projection_advances_server_rev(tmp_path):
    built = native_module_path('turn-projection-rev-adoption.js',
                               TURN_PROJECTION_TS)
    proc = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, built],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got['firstChanged'] is True, 'first projection must repaint'
    assert got['firstRev'] == 13639, (
        'the initial authoritative rev must be adopted')
    assert got['secondChanged'] is False, (
        'a rev-only snapshot must NOT repaint the unchanged document')
    assert got['secondRev'] == 14000, (
        'a rev-only snapshot must still advance _serverRev — this is the '
        'production frozen-rev defect')
    assert got['thirdRev'] == 14000, (
        'a stale/reordered frame must never lower _serverRev')
    assert got['needsLoad'] is False, (
        'a repainting projection still clears the stale-body marker')
    assert got['fourthChanged'] is True
    assert got['firstPhaseChanged'] is True
    assert got['heartbeatChanged'] is True, (
        'a new phase event sequence must repaint current-attempt HUD state')
    assert got['duplicateHeartbeatChanged'] is False, (
        'replaying the same phase sequence must not cause another repaint')
    assert got['serverMsgCount'] == 1
    assert got['transport'] == 'connected'
    assert got['hasTranscript'] is False
    assert got['hasPhase'] is False


if __name__ == '__main__':
    built = native_module_path('turn-projection-rev-adoption.js',
                               TURN_PROJECTION_TS)
    proc = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, built],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print(proc.stderr)
        raise SystemExit(1)
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    for key, expected in (
            ('firstChanged', True), ('firstRev', 13639),
            ('secondChanged', False), ('secondRev', 14000),
            ('thirdRev', 14000), ('needsLoad', False)):
        assert got[key] == expected, (key, got)
    print('ALL PASSED')
