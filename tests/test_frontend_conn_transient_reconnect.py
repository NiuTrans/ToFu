#!/usr/bin/env python3
"""Transient reconnecting vs. terminal offline — the connection-drop false positive.

WHY
---
Under a buffering proxy (the VS Code port-forward case) the SSE `done` event
gets swallowed, so the frontend's per-second stream timer sees a long silence.
The OLD code then pinged `/api/health`; a 3s timeout (or, on the wake path, a
SINGLE failure) flipped the verdict to "server offline" and stamped a TERMINAL
`finishReason='server_offline'` + a red error envelope on the assistant message
WITHOUT EVER POLLING THE TASK — even though the server was alive and the turn
had actually completed. That is a client-side lifecycle INFERENCE from a
transient transport signal — a violation of the project's pure-reducer
invariant (the frontend renders backend facts, it does not infer settled
lifecycle from client state).

THE FIX (Plan A), asserted here end-to-end against the REAL shipped functions
extracted from ``static/js/core/health_stream_timer.js`` +
``static/js/core/cross_tab_sync.js`` and eval'd in node:

  AC1  A failed health ping never stamps a terminal. It enters a TRANSIENT,
       non-persistent "reconnecting" state (``info._reconnecting=true`` + a calm
       banner) and keeps polling the task. No ``finishReason``, no error.
  AC1b The wake path (tablet unlock / online) with a still-running task aborts
       the stale SSE to resume via the Last-Event-ID cursor — it does NOT
       force-offline on the first failure.
  AC2  The terminal verdict comes ONLY from backend truth: a ``done`` poll lands
       the authoritative result (zero error); a background clean-``done`` adopts
       the server's completed message (never an ``interrupted`` stamp); only a
       genuinely non-clean / gone task stamps the honest ``interrupted``.
  AC3  On recovery, the stale ``server_offline`` BADGE (``finishReason``) is
       dropped — not merely the error text — so a completed answer never keeps a
       "connection lost / server offline" residue.

Each behaviour carries a NEUTER control proving the fix is load-bearing (the old
terminal-stamp path still stamps; the clean-vs-nonclean discriminator still
flips; poisoning the badge-drop leaves the residue).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
TIMER_JS = os.path.join(ROOT, 'static', 'js', 'core', 'health_stream_timer.js')
XTAB_JS = os.path.join(ROOT, 'static', 'js', 'core', 'cross_tab_sync.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _brace_match(src: str, open_pos: int) -> int:
    depth = 0
    j = open_pos
    while j < len(src):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise AssertionError('unbalanced braces')


def _extract_fn(src: str, fn_name: str) -> str:
    m = re.search(r'(?:async\s+)?function\s+' + re.escape(fn_name) + r'\s*\(', src)
    assert m, f'{fn_name} not found'
    i = src.find('{', m.end())
    return src[m.start():_brace_match(src, i)]


# Fakes shared by every driver — none of the real DOM / network is present.
_PREAMBLE = r'''
let activeConvId = null;          // → _setBubbleLiveness (real one) would no-op; we stub it anyway
let conversations = [];
const activeStreams = new Map();
const _streamTimers = new Map();
let _serverAlive = true, _consecutiveHealthFails = 0, _lastHealthCheck = 0;
const _HEALTH_CHECK_INTERVAL = 10000, _SILENCE_THRESHOLD = 20, _SILENCE_SEVERE = 45;
const _LIVENESS_ICON_WARN = '<svg warn>';
const _livenessLog = [];
const _events = [];
function _setBubbleLiveness(cid, html) { _livenessLog.push(html); }
function _connT(k, p) { return k; }
function escapeHtml(s) { return String(s == null ? '' : s); }
function twStop() {}
function saveConversations() {}
function renderChat() {}
function renderConversationList() {}
function connectToTask() { _events.push('connectToTask'); }
function finishStream() {}
function showToast() {}
function _startOfflineRecoveryPolling() {}
function normalizeErrorEnvelope(o) { return o; }
function errorEnvelopeKind(e) { return e && e.kind; }
const ConvCache = { put() {} };
let _healthReturn = false;
async function _checkServerHealth() { _consecutiveHealthFails++; return _healthReturn; }
let Api = {};
let _reattachLiveOfflineTask = () => false;
let _lastOfflineRecoveryAttempt = 0;
const _OFFLINE_RECOVERY_COOLDOWN = 5000;
const flush = () => new Promise(r => setTimeout(r, 0));
async function flushAll() { for (let i = 0; i < 10; i++) await flush(); }
'''


def _run(extracted: str, driver: str) -> dict:
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available for extraction-and-eval')
    harness = f'''
{_PREAMBLE}
{extracted}
(async () => {{
{driver}
}})();
'''
    with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        out = subprocess.run([node, tmp], capture_output=True, text=True, timeout=25)
        assert out.returncode == 0, f'node eval failed:\n{out.stderr}'
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(tmp)


# ── Timer-subsystem functions (AC1 / AC1b / AC2) ──
def _timer_fns(poison: str = '') -> str:
    src = _read(TIMER_JS)
    fns = ['_enterReconnecting', '_exitReconnecting', '_stampForcedOffline',
           '_forceFinishDeadStream', '_healStuckPlaceholder', '_probeStuckStream']
    body = '\n'.join(_extract_fn(src, f) for f in fns)
    if poison == 'clean_done':
        # Neuter the clean-vs-nonclean discriminator so EVERY terminal is
        # treated as non-clean → even a `done` would wrongly stamp interrupted.
        body = body.replace(
            "const _cleanDone = !probe.notFound",
            "const _cleanDone = false && !probe.notFound")
        assert 'false && !probe.notFound' in body, 'clean_done poison did not apply'
    return body


# ── cross_tab recovery function (AC3) ──
def _xtab_fn(poison: str = '') -> str:
    src = _read(XTAB_JS)
    body = _extract_fn(src, '_recoverOfflineConversations')
    if poison == 'badge_drop':
        body = body.replace('delete am.finishReason;', 'void 0;')
        assert 'void 0;' in body, 'badge_drop poison did not apply'
    return body


# ─────────────────────────── AC1: transient reconnecting ───────────────────────────

def test_health_fail_task_running_is_transient_no_terminal_stamp():
    """Health ping fails, task still running → reconnecting banner + NO stamp."""
    driver = '''
conversations = [{ id:'c1', activeTaskId:'t1',
  messages:[{ role:'assistant', content:'partial so far', toolRounds:[] }] }];
activeStreams.set('c1', { controller: { abort(){ _events.push('abort'); } } });
_streamTimers.set('c1', { lastDataTime: Date.now()-60000, _healthChecking:false });
_healthReturn = false;
Api = { chat:{ poll: async()=>({ ok:true, status:200, json: async()=>({ status:'running' }) }) },
        conversations:{ get: async()=>null } };
_probeStuckStream('c1', { silentSec:60 });
await flushAll();
const m = conversations[0].messages[0];
console.log(JSON.stringify({
  finishReason: m.finishReason || null,
  hasError: !!m.error,
  reconnectingBanner: _livenessLog.some(h => h && h.includes('conn.reconnecting')),
  infoReconnecting: !!_streamTimers.get('c1')._reconnecting,
}));
'''
    r = _run(_timer_fns(), driver)
    assert r['finishReason'] is None, 'must NOT stamp a terminal finishReason'
    assert r['hasError'] is False, 'must NOT stamp an error envelope'
    assert r['reconnectingBanner'] is True, 'calm reconnecting banner must be shown'
    assert r['infoReconnecting'] is True, 'transient reconnecting flag must be set'


def test_NC_forced_offline_path_still_stamps_terminal():
    """NEUTER: the terminal-stamp path (_stampForcedOffline, the old auto verdict)
    DOES set server_offline + error. Proves routing to reconnecting is the fix,
    not that stamping was already impossible."""
    driver = '''
conversations = [{ id:'c1', activeTaskId:'t1',
  messages:[{ role:'assistant', content:'partial', toolRounds:[] }] }];
_stampForcedOffline('c1');
const m = conversations[0].messages[0];
console.log(JSON.stringify({
  finishReason: m.finishReason || null,
  errorKind: (m.error && m.error.kind) || null,
}));
'''
    r = _run(_timer_fns(), driver)
    assert r['finishReason'] == 'server_offline'
    assert r['errorKind'] == 'server_offline'


# ─────────────────────────── AC1b: wake path reconnects ───────────────────────────

def test_wake_single_fail_running_reconnects_not_offline():
    """Wake + single health fail + running task → abort stale SSE to resume,
    NOT a terminal offline stamp."""
    driver = '''
conversations = [{ id:'c1', activeTaskId:'t1',
  messages:[{ role:'assistant', content:'partial', toolRounds:[] }] }];
const stream = { controller: { abort(){ _events.push('abort'); } } };
activeStreams.set('c1', stream);
_streamTimers.set('c1', { lastDataTime: Date.now()-5000, _healthChecking:false });
_healthReturn = false;   // first (and only) failure
Api = { chat:{ poll: async()=>({ ok:true, status:200, json: async()=>({ status:'running' }) }) },
        conversations:{ get: async()=>null } };
_probeStuckStream('c1', { wake:true, silentSec:5 });
await flushAll();
const m = conversations[0].messages[0];
console.log(JSON.stringify({
  finishReason: m.finishReason || null,
  hasError: !!m.error,
  aborted: _events.includes('abort'),
  probeAbort: !!stream._probeAbort,
}));
'''
    r = _run(_timer_fns(), driver)
    assert r['finishReason'] is None, 'wake single-fail must NOT force offline'
    assert r['hasError'] is False
    assert r['aborted'] is True and r['probeAbort'] is True, 'must abort stale SSE to resume via cursor'


# ─────────────────────────── AC2: terminal only from backend truth ───────────────────────────

def test_health_fail_task_done_lands_result_zero_error():
    """The reported main path: server alive but SSE swallowed, task already done
    → poll-fallback (abort stale SSE), zero error, no server_offline."""
    driver = '''
conversations = [{ id:'c1', activeTaskId:'t1',
  messages:[{ role:'assistant', content:'streamed so far', toolRounds:[] }] }];
const stream = { controller: { abort(){ _events.push('abort'); } } };
activeStreams.set('c1', stream);
_streamTimers.set('c1', { lastDataTime: Date.now()-60000, _healthChecking:false });
_healthReturn = false;
Api = { chat:{ poll: async()=>({ ok:true, status:200, json: async()=>({ status:'done' }) }) },
        conversations:{ get: async()=>null } };
_probeStuckStream('c1', { silentSec:60 });
await flushAll();
const m = conversations[0].messages[0];
console.log(JSON.stringify({
  finishReason: m.finishReason || null,
  hasError: !!m.error,
  aborted: _events.includes('abort'),
  probeAbort: !!stream._probeAbort,
  infoReconnecting: !!(_streamTimers.get('c1')||{})._reconnecting,
}));
'''
    r = _run(_timer_fns(), driver)
    assert r['finishReason'] != 'server_offline'
    assert r['hasError'] is False, 'a completed turn must never show an error'
    assert r['aborted'] is True and r['probeAbort'] is True, 'route to poll-fallback'
    assert r['infoReconnecting'] is False, 'reconnecting cleared once backend answered'


def test_background_clean_done_adopts_server_result_no_stamp():
    """Background heal on a clean `done` adopts the server's authoritative
    message (finishReason from server), never an interrupted stamp."""
    driver = '''
conversations = [{ id:'c1', activeTaskId:'t1',
  messages:[{ role:'assistant', content:'partial local', toolRounds:[] }] }];
_streamTimers.set('c1', { lastDataTime: Date.now(), _healthChecking:false });
Api = { chat:{ poll: async()=>({ ok:true }) },
        conversations:{ get: async()=>({ messages:[
          { role:'assistant', content:'FULL SERVER ANSWER', finishReason:'stop', usage:{t:1} }
        ] }) } };
_healStuckPlaceholder('c1', { status:'done', background:true });
await flushAll();
const m = conversations[0].messages[0];
console.log(JSON.stringify({
  content: m.content,
  finishReason: m.finishReason || null,
  hasError: !!m.error,
}));
'''
    r = _run(_timer_fns(), driver)
    assert r['content'] == 'FULL SERVER ANSWER', 'must adopt authoritative server content'
    assert r['finishReason'] == 'stop', "must adopt the server's real terminal reason"
    assert r['hasError'] is False
    assert r['finishReason'] != 'interrupted', 'a clean done is never interrupted'


def test_NC_neutered_clean_discriminator_stamps_interrupted_on_done():
    """NEUTER: poison the clean-vs-nonclean discriminator → a `done` is treated
    non-clean and stamps interrupted. Proves the discriminator is load-bearing."""
    driver = '''
conversations = [{ id:'c1', activeTaskId:'t1',
  messages:[{ role:'assistant', content:'partial local', toolRounds:[] }] }];
_streamTimers.set('c1', { lastDataTime: Date.now(), _healthChecking:false });
Api = { conversations:{ get: async()=>({ messages:[
          { role:'assistant', content:'FULL SERVER ANSWER', finishReason:'stop' } ] }) } };
_healStuckPlaceholder('c1', { status:'done', background:true });
await flushAll();
const m = conversations[0].messages[0];
console.log(JSON.stringify({ finishReason: m.finishReason || null, content: m.content }));
'''
    r = _run(_timer_fns(poison='clean_done'), driver)
    assert r['finishReason'] == 'interrupted', 'neutered discriminator stamps interrupted even on done'
    assert r['content'] == 'partial local', 'no server adopt on the poisoned path'


def test_background_nonclean_interrupted_stamps_honest_terminal():
    """A genuinely non-clean terminal (interrupted) with only partial local
    content stamps the honest interrupted verdict — this is NOT a false positive."""
    driver = '''
conversations = [{ id:'c1', activeTaskId:'t1',
  messages:[{ role:'assistant', content:'partial only', toolRounds:[] }] }];
_streamTimers.set('c1', { lastDataTime: Date.now(), _healthChecking:false });
Api = { conversations:{ get: async()=>null } };
_healStuckPlaceholder('c1', { status:'interrupted', background:true });
await flushAll();
const m = conversations[0].messages[0];
console.log(JSON.stringify({ finishReason: m.finishReason || null, hasError: !!m.error }));
'''
    r = _run(_timer_fns(), driver)
    assert r['finishReason'] == 'interrupted'
    assert r['hasError'] is False


# ─────────────────────────── AC3: recovery drops the stale badge ───────────────────────────

def test_recovery_drops_stale_server_offline_badge():
    """Server back online + no fresher terminal reason → the frontend-only
    server_offline BADGE is dropped, not just the error text."""
    driver = '''
conversations = [{ id:'c1', _needsLoad:false,
  messages:[{ role:'assistant', content:'complete answer', finishReason:'server_offline' }] }];
Api = { health:{ check: async()=>({ ok:true }) },
        chat:{ active: async()=>[] },
        conversations:{ get: async()=>({ messages:[
          { role:'assistant', content:'complete answer' } ] }) } };
await _recoverOfflineConversations('test');
const m = conversations[0].messages[0];
console.log(JSON.stringify({ finishReason: m.finishReason || null, hasError: !!m.error }));
'''
    r = _run(_xtab_fn(), driver)
    assert r['finishReason'] is None, 'stale server_offline badge must be cleared'
    assert r['hasError'] is False


def test_NC_neutered_badge_drop_leaves_residue():
    """NEUTER: poison the badge-drop → the server_offline badge stays. Proves the
    drop is load-bearing (recovery previously cleared only the error text)."""
    driver = '''
conversations = [{ id:'c1', _needsLoad:false,
  messages:[{ role:'assistant', content:'complete answer', finishReason:'server_offline' }] }];
Api = { health:{ check: async()=>({ ok:true }) },
        chat:{ active: async()=>[] },
        conversations:{ get: async()=>({ messages:[
          { role:'assistant', content:'complete answer' } ] }) } };
await _recoverOfflineConversations('test');
const m = conversations[0].messages[0];
console.log(JSON.stringify({ finishReason: m.finishReason || null }));
'''
    r = _run(_xtab_fn(poison='badge_drop'), driver)
    assert r['finishReason'] == 'server_offline', 'poisoned drop leaves the residue'
