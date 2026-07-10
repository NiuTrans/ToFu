"""Ground-truth: a CAS 409 (`blocked_rev_conflict`) must round-trip through the
REAL `syncConversationToServer` path — GET the authoritative server row,
append-missing-tail rebase the client's un-acked messages (by `_msgId`), and
re-PUT with the fresh `baseRev` — so NO message is clobbered or lost.

THE DIVERGENCE (what CAS actually catches)
------------------------------------------
Two tabs/devices share the conversation. Tab A appended a message and synced →
server rev advanced and the server now holds a message Tab B never saw. Tab B
(baseRev stale) then tries to PUT its OWN un-acked appended message. A blind
overwrite would erase Tab A's message (the timestamp-tiebreaker bug this epic
replaces). The correct behaviour: Tab B rebases — server messages are the
authoritative base, Tab B's un-acked tail is APPENDED by `_msgId` — so the final
server state contains BOTH.

This harness runs the REAL shipped `syncConversationToServer` + `_rebaseUnackedTail`
under node against a STATEFUL server stub that enforces CAS exactly like the
backend (`routes/conversations.py`): PUT with mismatched baseRev → 409
`blocked_rev_conflict` + serverRev; GET → the current server row incl. rev.

Asserts (#2 of the epic's step-#1 requirements):
  * the conflicting PUT is followed by a GET + a retry PUT (rebase round-trip);
  * the FINAL server state contains BOTH the server's message AND the client's
    un-acked message (no clobber, no loss), ordered server-base then appended;
  * each surviving message keeps its original `_msgId` verbatim.

NEUTER with teeth (#3): replace the rebase branch with a BLIND re-PUT (skip the
GET+append, just retry the client's original messages with the fresh baseRev) →
the server's message is LOST → the both-present assertion fails. Proves the
rebase — not the mere retry — is what preserves the data. Real file untouched.

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


# A stateful CAS server + the real syncConversationToServer driven against it.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── Stateful server that enforces CAS exactly like routes/conversations.py. ──
// Seeded diverged: server holds [u0, a1(server)] at rev=2; the client's baseRev
// will be 1 (stale), and the client holds its own un-acked [u0, a1, u2(client)].
const server = {
  messages: [
    { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
    { role: 'assistant', content: 'server-answer', timestamp: 1001, _msgId: 'm-a1-server' },
  ],
  rev: 2,
  title: 't',
};
const calls = { put: [], get: 0 };

global.Api = {
  conversations: {
    put: async (id, body) => {
      calls.put.push({ id, baseRev: body.baseRev, msgs: body.messages.map(m => m._msgId) });
      // CAS: reject a mismatched baseRev (message-bearing write).
      if (body.baseRev !== undefined && body.baseRev !== null && body.baseRev !== server.rev) {
        return { ok: false, status: 409,
                 clone() { return this; },
                 json: async () => ({ ok: false, error: 'blocked_rev_conflict',
                                      serverRev: server.rev, serverMsgCount: server.messages.length }) };
      }
      // Accept: adopt the client's messages, bump rev (messages changed).
      server.messages = body.messages.map(m => Object.assign({}, m));
      server.rev += 1;
      const newRev = server.rev;
      return { ok: true, status: 200, clone() { return this; },
               json: async () => ({ ok: true, rev: newRev }) };
    },
    get: async (id) => {
      calls.get += 1;
      return { messages: server.messages.map(m => Object.assign({}, m)),
               title: server.title, rev: server.rev };
    },
  },
};
global.activeStreams = new Map();
global.ConvCache = { put() {}, remove() {} };
global.debugLog = function() {};
global.config = { defaultThinkingDepth: 'medium' };
global.activeConvId = null;
global.renderChat = function() {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/conversations.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof syncConversationToServer !== 'function') {
  console.log('FAIL fn_exposed syncConversationToServer missing'); process.exit(0);
}

(async () => {
  // Client conv: stale baseRev=1, holds its OWN un-acked appended message u2
  // (which the server does not have) plus the shared prefix.
  const conv = {
    id: 'c-rev', title: 't',
    messages: [
      { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-u0' },
      { role: 'assistant', content: 'local-answer', timestamp: 1001, _msgId: 'm-a1-local' },
      { role: 'user', content: 'q2-unacked', timestamp: 2000, _msgId: 'm-u2-client' },
    ],
    _serverRev: 1,           // STALE (server is at 2)
    _serverMsgCount: 2,
    createdAt: 1699999000000, updatedAt: 1700000000000,
    model: 'aws.claude-opus-4.8',
  };

  const ok = await syncConversationToServer(conv);

  // The first PUT (baseRev=1) 409s; then a GET; then a retry PUT (baseRev=2).
  check('conflict_then_retry', calls.put.length === 2 && calls.get === 1);
  check('retry_used_fresh_baseRev', calls.put.length === 2 && calls.put[1].baseRev === 2);
  check('sync_succeeded', ok === true);

  // FINAL server state must contain BOTH the server's message AND the client's
  // un-acked message — no clobber, no loss.
  const ids = server.messages.map(m => m._msgId);
  check('server_msg_preserved', ids.includes('m-a1-server'));
  check('client_unacked_appended', ids.includes('m-u2-client'));
  // Append-missing-tail ordering: server base first, then the client-only tail.
  check('order_server_base_then_client_tail',
        ids.indexOf('m-a1-server') < ids.indexOf('m-u2-client'));
  // _msgId preserved verbatim (no reassignment → no spurious future rev bump).
  const u2 = server.messages.find(m => m._msgId === 'm-u2-client');
  check('msgId_verbatim', !!u2 && u2.content === 'q2-unacked');
  // Client adopted the final server rev for its next PUT.
  check('client_adopted_final_rev', conv._serverRev === server.rev);

  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL harness_threw ' + e.message + '\n' + e.stack); });
"""

# Neuter: rip out the rebase (GET + append-missing-tail) and do a BLIND re-PUT
# — just resend the client's original messages with the fresh baseRev. This is
# the "worse than the bug" behaviour the requirement forbids; it must LOSE the
# server's message.
_FIXED_MARKER = 'const rebased = _rebaseUnackedTail(serverMsgs, conv.messages);'
_NEUTER_REPLACEMENT = (
    'const rebased = conv.messages;  // NEUTER: blind re-PUT, skip append-missing-tail'
)


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_rev_rebase_harness.js')
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
def test_rev_conflict_rebase_preserves_both():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run_harness(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'rev-rebase failures:\n' + output
    assert output.count('PASS') >= 8, f'expected >=8 PASS, got:\n{output}'

    # Source-level guard: the rebase branch must exist and use append-missing-tail.
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()
    assert 'blocked_rev_conflict' in src and _FIXED_MARKER in src, (
        'regression: the rev-conflict rebase branch (GET + _rebaseUnackedTail + '
        're-PUT) is gone — a CAS 409 would fall through to a blind retry / no-op.')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_blind_reput_neuter_loses_server_message(tmp_path):
    """NEUTER with teeth: skip the append-missing-tail rebase (blind re-PUT of
    the client's own messages) → the server's message is LOST → the
    both-present assertion fails. Proves the rebase does the work. Real file
    untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()
    assert _FIXED_MARKER in src, 'fixed rebase marker not found — update the neuter target'
    neutered_src = src.replace(_FIXED_MARKER, _NEUTER_REPLACEMENT, 1)
    assert neutered_src != src, 'neuter did not change the source'
    nfile = tmp_path / 'conversations_neutered.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # The blind re-PUT MUST lose the server's message…
    assert lines.get('server_msg_preserved') is False, (
        'NEUTER did not bite: blind re-PUT still preserved the server message — '
        'the test does not discriminate the rebase.\n' + output)
    # …while the client's own message still lands (it was in the blind PUT).
    assert lines.get('client_unacked_appended') is True, (
        'unexpected: neuter also dropped the client message:\n' + output)
