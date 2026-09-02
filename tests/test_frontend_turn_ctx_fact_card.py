"""tests/test_frontend_turn_ctx_fact_card.py — the turn-ctx capsule is
a FACT card, not a UI snapshot.

WHY
---
The per-turn note in the message right-gutter (rendered by
``frontend/src/runtime/app-runtime.js`` info-rail section) is captured from
the LIVE toolbar at send
time — the user can then pause in the composer and switch preset or
toggle a mode before the stream actually starts, and the note used to
freeze whatever was live at send. That was misleading:

  • Send-time snapshot said ``Claude Opus 4.8``.
  • The turn actually ran on ``claude-opus-4.7`` (preset switched during
    the send-to-stream pause, or dispatcher fell back).
  • Nothing corrected the note → the gutter capsule lied.

The fix reframes the note as a two-phase FACT card: send-time is a
best-effort snapshot; DONE ships the server-authoritative
``actualModel`` / ``actualDepth`` / ``actualModes``, and
``reconcileTurnCtxCapsule`` OVERWRITES those fields in place. This
harness extracts the REAL shipped ``info-rail.js`` section under bare node and
asserts the fact-card overwrite.

Failing-first: constructs a case where the send snapshot's model
differs from the done frame's ``actualModel`` and asserts the reconcile
mutates the snapshot.

NEUTER: on a mutated COPY of that section (the shipped file is
untouched), stripping the ``snap.model = fact.actualModel`` assignment
makes the fact-card assertion FAIL — proving the overwrite is
load-bearing.

"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
APP_RUNTIME = os.path.join(ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


# NOTE: keep this harness self-contained — info-rail.js is loaded as raw
# text, wrapped in an IIFE the module already ships as (0, eval)-safe.
# Escaping: this is a Python raw string so \\ stays a literal backslash
# in JS. All %-style formatting is done in Python before write.
_HARNESS = r"""
'use strict';
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
const out = [];
function check(name, cond, extra) {
  const line = (cond ? 'PASS ' : 'FAIL ') + name + (extra ? ' :: ' + extra : '');
  out.push(line);
}

// The migrated info-rail section reads these globals via `typeof`. Provide inert values so
// buildTurnCtxSnapshot/_collect* don't throw on load — the harness only
// exercises reconcileTurnCtxCapsule, which is a pure function over `snap`
// + `fact` and needs no live UI state.
global.escapeHtml = (s) => String(s);
global.projectState = { active: false, path: '' };
global.searchMode = 'off';
global.fetchEnabled = false;
global.browserEnabled = false;
global.desktopEnabled = false;
global.codeExecEnabled = false;
global.memoryEnabled = false;
global.imageGenEnabled = false;
global.humanGuidanceEnabled = false;
global.autoTranslate = false;
global.autopilotEnabled = false;
global.swarmEnabled = false;
global.activeFlow = '';
global.config = { model: '', thinkingDepth: '' };
global.serverModel = '';
global._isThinkingCapable = () => false;
global._modelShortName = (m) => m;
global._detectBrand = () => 'generic';
global._brandSvg = () => '';
global.Api = { mcp: { toolsList: async () => ({ ok: false }) } };
global.document = { readyState: 'complete', addEventListener: () => {} };

const SRC_PATH = process.argv[2];
const BUNDLE_SRC = fs.readFileSync(SRC_PATH, 'utf8');
const START = '/* ===== migrated source: info-rail.js ===== */';
const END = '/* ===== migrated source: local-control.js ===== */';
const startAt = BUNDLE_SRC.indexOf(START);
const endAt = BUNDLE_SRC.indexOf(END, startAt + START.length);
if (startAt < 0 || endAt < 0) {
  throw new Error('migrated info-rail section markers missing');
}
const SRC = BUNDLE_SRC.slice(startAt, endAt);
function loadModule(src){ (0, eval)(src); }
loadModule(SRC);

if (typeof reconcileTurnCtxCapsule !== 'function') {
  console.log('FAIL fn_exposed reconcileTurnCtxCapsule missing');
  console.log(out.join('\n'));
  process.exit(0);
}
check('fn_exposed', true);

// ── FACT-CARD OVERWRITE ──────────────────────────────────────────────
// User sent with Claude Opus 4.8 / high depth / Autopilot mode.
// During the send-to-stream pause they switched preset → server actually
// answered with claude-opus-4.7 / medium depth / no Autopilot.
function _mkSnap() {
  return {
    roots: [],
    tools: [{ label: 'Search', tone: 'search' }],
    modes: [{ label: 'Autopilot', tone: 'mode' }],
    model: 'claude-opus-4.8',
    depth: 'high',
  };
}
{
  const snap = _mkSnap();
  const fact = {
    actualModel: 'claude-opus-4.7',
    actualDepth: 'medium',
    actualModes: [{ label: 'Swarm', tone: 'mode' }],
  };
  const changed = reconcileTurnCtxCapsule(snap, fact);
  check('fact_overwrite_returns_true', changed === true);
  check('fact_overwrite_model', snap.model === 'claude-opus-4.7',
        'model=' + snap.model);
  check('fact_overwrite_depth', snap.depth === 'medium',
        'depth=' + snap.depth);
  check('fact_overwrite_modes_len', Array.isArray(snap.modes) && snap.modes.length === 1);
  check('fact_overwrite_modes_label',
        snap.modes[0] && snap.modes[0].label === 'Swarm',
        'modes=' + JSON.stringify(snap.modes));
  // tools are outside the fact-card contract and remain untouched.
  check('fact_no_touch_tools',
        snap.tools.length === 1 && snap.tools[0].label === 'Search');
}

// ── PARTIAL fact updates leave unrelated fields untouched ───────────
{
  const snap = _mkSnap();
  const changed = reconcileTurnCtxCapsule(snap, { actualModel: 'gpt-5.6-sol' });
  check('fact_only_model_changed', changed === true && snap.model === 'gpt-5.6-sol');
  // No diff → tools/modes unchanged, depth unchanged
  check('fact_only_no_side_effects',
        snap.tools.length === 1 && snap.modes.length === 1 && snap.depth === 'high');
}

// ── EMPTY actualModes replaces the modes list (fact wins verbatim) ──
{
  const snap = _mkSnap();
  const changed = reconcileTurnCtxCapsule(snap, { actualModes: [] });
  check('fact_empty_modes_replaces',
        changed === true && Array.isArray(snap.modes) && snap.modes.length === 0);
}

// ── actualDepth === '' clears the depth chip ─────────────────────────
{
  const snap = _mkSnap();
  const changed = reconcileTurnCtxCapsule(snap, { actualDepth: '' });
  check('fact_empty_depth_clears', changed === true && snap.depth === '');
}

// ── NO-OP: identical fact returns false (nothing changed) ────────────
{
  const snap = _mkSnap();
  const changed = reconcileTurnCtxCapsule(snap, {
    actualModel: 'claude-opus-4.8',
    actualDepth: 'high',
    actualModes: [{ label: 'Autopilot', tone: 'mode' }],
  });
  check('noop_identical_fact_returns_false', changed === false);
}

// ── NEUTER: strip the model-overwrite line → fact_overwrite_model flips red ─
{
  const NEEDLE = 'snap.model = fact.actualModel;';
  const neutered = SRC.replace(NEEDLE, '/* neutered */');
  check('neuter_applied', neutered !== SRC,
        'search string missing from SRC — refresh NEEDLE');
  // Load the neutered module into a fresh sandbox so it doesn't clobber
  // the shipped one already exposed on `window`.
  const sandbox = { window: {} };
  sandbox.runtimeScope = sandbox.window;
  sandbox.escapeHtml = global.escapeHtml;
  sandbox.projectState = global.projectState;
  sandbox.searchMode = global.searchMode;
  sandbox.fetchEnabled = global.fetchEnabled;
  sandbox.browserEnabled = global.browserEnabled;
  sandbox.desktopEnabled = global.desktopEnabled;
  sandbox.codeExecEnabled = global.codeExecEnabled;
  sandbox.memoryEnabled = global.memoryEnabled;
  sandbox.imageGenEnabled = global.imageGenEnabled;
  sandbox.humanGuidanceEnabled = global.humanGuidanceEnabled;
  sandbox.autoTranslate = global.autoTranslate;
  sandbox.autopilotEnabled = global.autopilotEnabled;
  sandbox.swarmEnabled = global.swarmEnabled;
  sandbox.activeFlow = global.activeFlow;
  sandbox.config = global.config;
  sandbox.serverModel = global.serverModel;
  sandbox._isThinkingCapable = global._isThinkingCapable;
  sandbox._modelShortName = global._modelShortName;
  sandbox._detectBrand = global._detectBrand;
  sandbox._brandSvg = global._brandSvg;
  sandbox.Api = global.Api;
  sandbox.document = global.document;
  const vm = require('vm');
  vm.createContext(sandbox);
  vm.runInContext(neutered, sandbox);
  const rec = sandbox.window.reconcileTurnCtxCapsule;
  check('neuter_exposes_fn', typeof rec === 'function');
  const snap = { model: 'A', depth: 'high', modes: [], tools: [], roots: [] };
  rec(snap, { actualModel: 'B' });
  // With the model-overwrite line stripped, snap.model must stay 'A'.
  check('neuter_model_not_overwritten', snap.model === 'A',
        'after neuter snap.model=' + snap.model);
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_turn_ctx_fact_card_and_reconcile():
    harness = os.path.join(HERE, '_turn_ctx_fact_card_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, APP_RUNTIME],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [l for l in output.splitlines() if l.startswith('FAIL')]
    assert not fails, 'turn-ctx fact-card failures:\n' + output
    # Sanity: assert the total number of PASS lines matches the harness so
    # a silently-skipped case can't slip through as a false green.
    passes = [l for l in output.splitlines() if l.startswith('PASS')]
    assert len(passes) >= 15, (
        f'expected >=15 PASS lines, got {len(passes)}:\n{output}')
