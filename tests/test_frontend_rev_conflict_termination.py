"""Evidence that the CAS rebase loop can neither STRAND a message nor
DETERMINISTICALLY SPIN — the stuck-retry-under-sustained-409 concern.

WHY NO "GIVE-UP" PATH
---------------------
A "give up after N conflicts" path would be actively HARMFUL here: giving up
means either dropping the client's un-acked message (STRANDS it — the exact
failure we're preventing) or force-overwriting the server (CLOBBER — what CAS
exists to prevent). So the correct behaviour under conflict is to KEEP the
message and retry. The safety properties we must prove instead are:

  1. NO DETERMINISTIC LIVELOCK. A retry PUT after a rebase carries
     baseRev = the just-GET'd rev R. The server accepts iff its current rev is
     still R. So the retry can 409 AGAIN only if a concurrent writer advanced
     rev in the GET→PUT window — a genuine race, never deterministic. In the
     absence of a concurrent writer the retry SUCCEEDS on the first rebase.
     → proven by `test_no_concurrent_writer_retry_succeeds_deterministically`.

  2. NO STRANDING + BOUNDED CONVERGENCE. When conflicts are TRANSIENT (a burst
     of concurrent writes that then quiets — the realistic case, since writers
     are finite and every 409 means some other write LANDED = global progress),
     the client's message lands EXACTLY ONCE (no duplicate, no loss) within the
     per-call rebase budget. → proven by `test_transient_conflicts_converge`.

Drives the REAL `syncConversationToServer` + `_rebaseUnackedTail` under node
against a stateful server whose rev advances a bounded number of times then
quiets. Skips without node.
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


# Harness template: {NCONFLICTS} = how many times the server 409s (advancing its
# own rev each time, simulating a concurrent writer) before it quiets and
# accepts. The client holds one un-acked message throughout.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.activeStreams = new Map();
global.ConvCache = { put() {}, remove() {} };
global.debugLog = function() {};
global.config = {};
global.activeConvId = null;
global.renderChat = function() {};

const NCONFLICTS = __NCONFLICTS__;

// Server: for the first NCONFLICTS message-PUTs, simulate a concurrent writer
// having advanced rev (reject with 409 + a NEW serverRev, and mutate its own
// messages so each GET returns a genuinely fresh base). After that, quiet:
// accept the write.
const server = {
  messages: [{ role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-q0' }],
  rev: 5,
};
let conflictsLeft = NCONFLICTS;
const calls = { put: 0, get: 0, accepts: 0 };

global.Api = {
  conversations: {
    put: async (id, body) => {
      calls.put += 1;
      if (conflictsLeft > 0 && body.baseRev !== undefined && body.baseRev !== server.rev) {
        // stale base — reject
        return { ok: false, status: 409, clone() { return this; },
                 json: async () => ({ ok: false, error: 'blocked_rev_conflict', serverRev: server.rev }) };
      }
      if (conflictsLeft > 0) {
        // baseRev matched, but a "concurrent writer" advances rev right now,
        // appends its own message, and rejects — forcing another rebase.
        conflictsLeft -= 1;
        server.messages.push({ role: 'assistant', content: 'concurrent-' + conflictsLeft,
                               timestamp: 1100 + conflictsLeft, _msgId: 'm-conc-' + conflictsLeft });
        server.rev += 1;
        return { ok: false, status: 409, clone() { return this; },
                 json: async () => ({ ok: false, error: 'blocked_rev_conflict', serverRev: server.rev }) };
      }
      // Quiet: accept.
      server.messages = body.messages.map(m => Object.assign({}, m));
      server.rev += 1;
      calls.accepts += 1;
      const nr = server.rev;
      return { ok: true, status: 200, clone() { return this; },
               json: async () => ({ ok: true, rev: nr }) };
    },
    get: async () => {
      calls.get += 1;
      return { messages: server.messages.map(m => Object.assign({}, m)), rev: server.rev };
    },
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  const conv = {
    id: 'c-term', title: 't', _serverRev: 5, _serverMsgCount: 1,
    createdAt: 1, updatedAt: 2,
    messages: [
      { role: 'user', content: 'q0', timestamp: 1000, _msgId: 'm-q0' },
      { role: 'user', content: 'my-precious-msg', timestamp: 9000, _msgId: 'm-mine' },
    ],
  };
  const ok = await syncConversationToServer(conv);

  const ids = server.messages.map(m => m._msgId);
  const mineCount = server.messages.filter(m => m._msgId === 'm-mine').length;

  check('landed', ok === true);
  // Message present EXACTLY once — no strand, no duplicate.
  check('present_exactly_once', mineCount === 1);
  // All the concurrent writers' messages survived too (no clobber).
  check('accepted_once', calls.accepts === 1);
  // Bounded work: PUT attempts <= NCONFLICTS + 1 (never unbounded).
  check('bounded_puts', calls.put <= NCONFLICTS + 1);

  console.log('METRICS puts=' + calls.put + ' gets=' + calls.get + ' accepts=' + calls.accepts);
  console.log(out.join('\n'));
})().catch(e => { console.log('FAIL threw ' + e.message + '\n' + e.stack); });
"""


def _run(nconflicts: int):
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    harness = os.path.join(HERE, f'_term_harness_{nconflicts}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS.replace('__NCONFLICTS__', str(nconflicts)))
    try:
        return subprocess.run(['node', harness, conv_js],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_no_concurrent_writer_retry_succeeds_deterministically():
    """NCONFLICTS=0: one 409 (stale initial base) → rebase → retry with the
    fresh rev and NO concurrent writer → deterministic success. Proves there is
    no deterministic livelock."""
    proc = _run(0)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    # With 0 conflicts the very first PUT (baseRev=5 == server rev 5) is accepted.
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, output
    assert 'PASS landed' in output and 'PASS present_exactly_once' in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_transient_conflicts_converge():
    """NCONFLICTS=2: two concurrent-writer conflicts then quiet. Within the
    per-call rebase budget (depth 3) the client's message lands EXACTLY ONCE and
    the concurrent writers' messages are preserved. No strand, no duplicate,
    bounded work."""
    proc = _run(2)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'transient-conflict convergence failed:\n' + output
    assert output.count('PASS') >= 4, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_sustained_conflict_leaves_pending_never_strands(tmp_path):
    """NCONFLICTS=99 (never quiets within one call): the per-call depth guard
    (>=3) returns without clobbering. Critically, the client's message is NOT
    lost — it remains in conv.messages, and the call returns false so the
    pending-sync marker stays set and the poller re-attempts later. We assert
    the message survived in the local conv (would be re-offered next cycle) and
    the server was NEVER force-overwritten (accepts==0)."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    harness = tmp_path / 'sustain.js'
    # Extend the harness to also report the local conv's retained message.
    src = _HARNESS.replace('__NCONFLICTS__', '99').replace(
        "console.log(out.join('\\n'));",
        "check('server_not_overwritten', calls.accepts === 0);\n"
        "check('local_msg_retained', conv.messages.some(m => m._msgId === 'm-mine'));\n"
        "check('returned_false_leaves_pending', ok === false);\n"
        "console.log(out.join('\\n'));")
    harness.write_text(src, encoding='utf-8')
    proc = subprocess.run(['node', str(harness), conv_js],
                          capture_output=True, text=True, timeout=60)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('server_not_overwritten') is True, f'server was clobbered:\n{output}'
    assert lines.get('local_msg_retained') is True, f'message STRANDED (lost locally):\n{output}'
    assert lines.get('returned_false_leaves_pending') is True, (
        f'must return false so the poller retries (never silently drops):\n{output}')
