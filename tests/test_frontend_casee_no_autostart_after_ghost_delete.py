"""Regression (patch→fundamental-fix #2): `initActiveTasks` must NEVER
auto-start an assistant turn from orphan detection.

WHY
---
The old Case-E path INFERRED an orphaned trailing-user turn from an age<5min
heuristic (on loaded messages OR a stale `_needsLoad` shell's
`settings.lastMsgRole`) and then AUTO-STARTED a billed LLM turn behind a 3s
`setTimeout`. Two failure modes:
  * a client-side inference minting a costly, hard-to-reverse action; and
  * on a stale shell, a DOUBLE-ANSWER — a second billed turn on top of an
    answer the server already produced (the shell metadata lagged the real DB).

THE FUNDAMENTAL FIX
-------------------
`initActiveTasks` no longer contains ANY auto-dispatch or age heuristic: the
framework never mints a billed LLM turn from a client-side inference about an
orphaned user turn. (An explicit backend-marked Resume affordance briefly
replaced the auto-fire, then was removed entirely — leaving the invariant this
test guards: NO auto-dispatch, full stop.)

This test drives the REAL shipped `initActiveTasks` end-to-end under node and
asserts `startAssistantResponse` is called ZERO times — for BOTH a ghost conv
(whose old delete-branch used to expose a trailing user) AND a genuine
trailing-user orphan conv (which the old Case-E would have auto-started). The
frontend buried-ghost sweep + empty-tail delete were RETIRED (2026-07-07): that
verdict is now backend-authoritative (routes/conversations.py GET-path reconcile
→ lib/conversations/reconcile.py, proven byte-equivalent by
tests/test_reconcile_js_backend_equivalence.py). So with the backend stubbed out
in this harness the ghost tail STAYS (the client no longer truncates); it just
never triggers a turn either.

DOUBLE-NEUTER (run below): re-inject an auto-fire into a COPY of
main_init_tasks.js (append a Case-E-style `startAssistantResponse` loop) and
prove the spy now records a call — i.e. the test genuinely discriminates "no
auto-dispatch" from "auto-dispatch present". Real file untouched.

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


# Drives the REAL initActiveTasks. Two convs, both LOADED, no activeTaskId, no
# running server task:
#  • conv-ghost: trailing EMPTY assistant ghost on top of a recent user msg.
#    Case D must delete the ghost; it must NOT auto-start anything.
#  • conv-orphan: a genuine trailing recent user msg. The OLD Case-E would have
#    auto-started this; the fixed code must NOT. The conv is simply left with
#    its unanswered trailing user turn — no auto-dispatch.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

const NOW = Date.now();
const RECENT = NOW - 30 * 1000;  // 30s ago (would be within the OLD 5-min window)

function _seedConvs() {
  return [
    {
      id: 'conv-ghost', title: 'ghost', _needsLoad: false, activeTaskId: null,
      messages: [
        { role: 'user', content: 'do the thing', timestamp: RECENT },
        { role: 'assistant', content: '', thinking: '', toolRounds: [], timestamp: RECENT + 1 },
      ],
      _serverMsgCount: 2,
    },
    {
      id: 'conv-orphan', title: 'orphan', _needsLoad: false, activeTaskId: null,
      messages: [
        { role: 'user', content: 'answer me', timestamp: RECENT },
      ],
      _serverMsgCount: 1,
    },
  ];
}

const started = [];   // convIds passed to startAssistantResponse (must stay EMPTY)
let conversations = [];
global.__reseed = () => { conversations = _seedConvs(); global.conversations = conversations; };
global.__reseed();
global.activeConvId = null;

global.loadConversationsFromServer = async () => {};
global.loadFolders = async () => {};
global.loadConversationMessages = async () => {};
global.Api = {
  chat: {
    activeResponse: async () => ({ ok: true, json: async () => [] }),  // NO running tasks
    poll: async () => ({ ok: true, json: async () => ({ status: 'done' }) }),
    active: async () => [],
  },
  conversations: { get: async () => null, put: async () => ({ ok: true }) },
};
global.startAssistantResponse = (convId) => { started.push(convId); };
global.connectToTask = () => {};
global.syncConversationToServer = async () => {};
global.saveConversations = () => {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.getActiveConv = () => null;
global.ConvCache = { put() {}, remove() {} };
global.debugLog = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.normalizeErrorEnvelope = (e) => e;
global.errorEnvelopeKind = () => '';
global._ensureMsgId = (m) => m;
global._migratePinnedToFolder = () => {};
global._refreshServerQueue = () => {};
global.isBranchTaskId = () => false;
global.initBranchReconnect = () => {};
global.config = { model: 'aws.claude-opus-4.8' };
global.serverModel = 'aws.claude-opus-4.8';
global.activeStreams = new Map();
global.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global._editingMsgIdx = null;
global.showStreamingUIForConv = () => {};

// If any setTimeout-deferred dispatch survived (it must NOT), fire it
// synchronously so the spy would catch it within the test.
global.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };
global.clearTimeout = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // main/main_init_tasks.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof initActiveTasks !== 'function') { console.log('FAIL fn_exposed initActiveTasks missing'); return; }
  check('fn_exposed', true);

  await initActiveTasks();
  for (let i = 0; i < 50; i++) { await Promise.resolve(); }

  // The frontend NO LONGER truncates: the buried-ghost sweep + empty-tail
  // delete were retired (2026-07-07) — that verdict is now backend-authoritative
  // (routes/conversations.py GET-path reconcile). With the backend stubbed out
  // here, the empty ghost tail STAYS untouched; the client never pops it.
  const ghost = conversations.find(c => c.id === 'conv-ghost');
  check('ghost_not_frontend_truncated', !!ghost && ghost.messages.length === 2
        && ghost.messages[1].role === 'assistant');

  // …and NOTHING auto-starts — not the ghost conv, not the genuine orphan.
  // This is the fundamental fix: no billed turn from a client-side inference.
  check('no_autostart_at_all', started.length === 0);

  console.log('STARTED=' + JSON.stringify(started));
  console.log(out.join('\n'));
})();
"""


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_casee_ghost_delete_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_source_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_casee_no_autostart_at_all():
    src_js = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')
    proc = _run_harness(src_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Case-E auto-dispatch regressions:\n' + output
    assert 'PASS no_autostart_at_all' in output, (
        'expected no_autostart_at_all to PASS:\n' + output)
    assert 'PASS ghost_not_frontend_truncated' in output, (
        'frontend must NOT truncate the ghost tail — the backend GET-path '
        'reconcile owns that now:\n' + output)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_casee_no_autostart_double_neuter(tmp_path):
    """DOUBLE-NEUTER: re-inject a Case-E-style auto-fire into a COPY of
    main_init_tasks.js and prove the spy now records a call — i.e. the test
    genuinely discriminates "no auto-dispatch" from "auto-dispatch present".
    Real file untouched."""
    src_js = os.path.join(JS_DIR, 'main', 'main_init_tasks.js')
    with open(src_js, encoding='utf-8') as f:
        src = f.read()

    # Re-inject the deleted auto-fire: right before the _bgRecovery `.then`, add
    # a loop that auto-starts any conv whose trailing message is a user turn.
    # This reproduces the OLD Case-E behaviour the fix removed.
    anchor = "    /* Fire background recovery — don't await it */"
    assert anchor in src, 'bgRecovery anchor not found — update the neuter target'
    inject = (
        "    for (const _c of conversations) {\n"
        "      const _lm = _c.messages[_c.messages.length - 1];\n"
        "      if (_lm && _lm.role === 'user') startAssistantResponse(_c.id);\n"
        "    }\n"
    )
    neutered_src = src.replace(anchor, inject + anchor, 1)
    assert neutered_src != src, 'neuter did not change the source'
    nfile = tmp_path / 'main_init_tasks_neutered.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # With an auto-fire re-injected, the spy records a call → the invariant FAILS.
    assert lines.get('no_autostart_at_all') is False, (
        'DOUBLE-NEUTER did not bite: re-injecting a Case-E auto-fire did NOT '
        'cause any startAssistantResponse call — the test does not discriminate '
        'the fix.\n' + output)
