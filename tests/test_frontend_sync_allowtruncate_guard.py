"""Regression: `syncConversationToServer`'s count-drop guard (the
`_serverMsgCount` data-loss protection) must HONOUR `allowTruncate:true`.

WHY
---
A caller that passes `allowTruncate:true` has DELIBERATELY reduced
`conv.messages` — a ghost / buried-ghost sweep (`initActiveTasks` Case D,
`main_init_tasks.js`), a Case-D empty-ghost delete, or an edit/regen
truncation. It calls `syncConversationToServer(conv, {allowTruncate:true})` to
PERSIST the shorter list.

But the guard at `conversations.js`:

    if (conv._serverMsgCount && conv.messages.length < conv._serverMsgCount) return;

ran FIRST and WITHOUT consulting `allowTruncate` (which only took effect ~130
lines later, at the Layer-1 staleness check + the PUT body). So a truncating
sync BAILED before ever reaching the PUT. Net effect for the same-day
buried-ghost fix: ghosts were swept from the DOM on each load but NEVER removed
server-side → they RESURRECTED on every reload. That is the exact
"chatInner shows stale/ghost elements out of sync with the backend" class the
sync objective targets, and it made an already-shipped fix INERT.

THE FIX
-------
Gate the count-drop guard on `!allowTruncate`, mirroring the Layer-1 staleness
check below it (`if (!allowTruncate && conv.messages.length > lightMsgs.length)`)
and the backend `allow_truncate` bypass (routes/conversations.py). The
stale-async-overwrite race path (see the `stale-async-sync-overwrite-msg-
regression` skill) never sets `allowTruncate`, so it stays protected.

This drives the REAL shipped `syncConversationToServer` under node, stubbing
only the network seam (`Api.conversations.put`), and asserts:
  (1) allowTruncate:true + (messages < _serverMsgCount) → PUT FIRES with the
      shorter body AND carries the allowTruncate flag.
  (2) default (no allowTruncate) + (messages < _serverMsgCount) → PUT is
      BLOCKED (the data-loss guard still protects the accidental race).
  (3) allowTruncate:true + (messages >= _serverMsgCount) → PUT fires (no
      regression for the normal grow/equal case).

DOUBLE-NEUTER (run below): reverting the guard to the un-gated form
(`if (conv._serverMsgCount && ...)`) makes check (1) FAIL — the truncating sync
is blocked again, i.e. the ghost-resurrect bug returns — while (2) stays green.

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


_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── Network seam: capture every PUT (the thing the guard gates). ──
const calls = { put: [] };
global.Api = {
  conversations: {
    put: async (id, body) => { calls.put.push({ id, body }); return { ok: true }; },
  },
};
global.activeStreams = new Map();
global.ConvCache = { put() {}, remove() {} };
global.debugLog = function() {};
global.config = { defaultThinkingDepth: 'medium' };
global.activeConvId = null;

// syncConversationToServer is a large top-level `async function` in
// conversations.js alongside many siblings. eval the WHOLE file so the REAL
// function is defined against our stubs; the other decls are harmless here.
eval(fs.readFileSync(process.argv[2], 'utf8'));  // core/conversations.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof syncConversationToServer !== 'function') {
  console.log('FAIL fn_exposed syncConversationToServer missing'); process.exit(0);
}
check('fn_exposed', true);

function _mkConv(id, nMsgs, serverCount) {
  const messages = [];
  for (let i = 0; i < nMsgs; i++) {
    messages.push({ role: i % 2 === 0 ? 'user' : 'assistant',
                    content: 'm' + i, timestamp: 1000 + i });
  }
  return {
    id, title: 't', messages,
    _serverMsgCount: serverCount,
    createdAt: 1699999000000, updatedAt: 1700000000000,
    // minimal settings inputs syncConversationToServer reads:
    model: 'aws.claude-opus-4.8',
  };
}

(async () => {
  // ── (1) allowTruncate:true + messages(2) < _serverMsgCount(6) → PUT FIRES. ──
  {
    calls.put.length = 0;
    const conv = _mkConv('c-trunc', 2, 6);   // swept from 6 down to 2
    await syncConversationToServer(conv, { allowTruncate: true });
    const put = calls.put[0];
    check('allowTruncate_put_fires', calls.put.length === 1);
    check('allowTruncate_put_shorter_body', !!put && put.body.messages.length === 2);
    check('allowTruncate_flag_sent', !!put && put.body.allowTruncate === true);
  }

  // ── (2) default (NO allowTruncate) + messages(2) < _serverMsgCount(6) →
  //    BLOCKED (the accidental-stale-overwrite guard still protects). ──
  {
    calls.put.length = 0;
    const conv = _mkConv('c-guard', 2, 6);
    await syncConversationToServer(conv);   // no opts → allowTruncate=false
    check('default_drop_blocked', calls.put.length === 0);
  }

  // ── (3) allowTruncate:true + messages(6) >= _serverMsgCount(6) → PUT fires
  //    (no regression for the normal grow/equal path). ──
  {
    calls.put.length = 0;
    const conv = _mkConv('c-equal', 6, 6);
    await syncConversationToServer(conv, { allowTruncate: true });
    check('equal_count_put_fires', calls.put.length === 1);
  }

  // ── (4) default + messages(7) > _serverMsgCount(6) → PUT fires (growth is
  //    always allowed regardless of the flag). ──
  {
    calls.put.length = 0;
    const conv = _mkConv('c-grow', 7, 6);
    await syncConversationToServer(conv);
    check('growth_put_fires', calls.put.length === 1);
  }

  console.log(out.join('\n'));
})();
"""


def _run_harness(js_source_path: str):
    harness = os.path.join(HERE, '_sync_allowtruncate_harness.js')
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
def test_sync_allowtruncate_guard():
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    proc = _run_harness(conv_js)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'sync allowTruncate-guard failures:\n' + output
    assert output.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{output}'

    # ── Source-level guard: the count-drop guard must be gated on !allowTruncate. ──
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()
    assert '!allowTruncate && conv._serverMsgCount && conv.messages.length < conv._serverMsgCount' in src, (
        'regression: the syncConversationToServer count-drop guard no longer '
        'honours allowTruncate — a deliberate truncation (ghost sweep / Case-D '
        'delete / edit-regen) is blocked, so swept ghosts RESURRECT on reload.')


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_sync_allowtruncate_guard_double_neuter(tmp_path):
    """DOUBLE-NEUTER: revert the guard to its un-gated form in a COPY of
    conversations.js and prove the truncating-sync check FAILS (the ghost-
    resurrect bug returns), while the default-blocked check stays green. Proves
    the test genuinely discriminates the fix. The real file is untouched."""
    conv_js = os.path.join(JS_DIR, 'core', 'conversations.js')
    with open(conv_js, encoding='utf-8') as f:
        src = f.read()
    fixed = '!allowTruncate && conv._serverMsgCount && conv.messages.length < conv._serverMsgCount'
    neutered = 'conv._serverMsgCount && conv.messages.length < conv._serverMsgCount'
    assert fixed in src, 'fixed guard text not found — update the neuter target'
    neutered_src = src.replace(fixed, neutered, 1)
    assert neutered_src != src, 'neuter did not change the source'
    nfile = tmp_path / 'conversations_neutered.js'
    nfile.write_text(neutered_src, encoding='utf-8')

    proc = _run_harness(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    # The neuter MUST break the truncating-sync path (PUT no longer fires)…
    assert lines.get('allowTruncate_put_fires') is False, (
        'DOUBLE-NEUTER did not bite: with the un-gated guard the truncating '
        'sync STILL fired a PUT — the test does not discriminate the fix.\n' + output)
    # …while the accidental-drop protection stays green (guard still blocks).
    assert lines.get('default_drop_blocked') is True, (
        'neuter unexpectedly broke the default-drop protection:\n' + output)
