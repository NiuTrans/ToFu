"""Regression: a crash-interrupted conversation must remain VISIBLE in the
sidebar after a server restart — it must not become a message-less shell that
is present in memory yet filtered out of the list (appearing "only when
clicked").

WHY
---
The sidebar shell built by ``loadConversationsFromServer`` (and
``hydrateSidebarFromCache``) in ``static/js/core/conversations.js`` derives the
visibility gate — ``_serverMsgCount`` / ``_needsLoad`` — from the server row's
message count. ``renderConversationList`` drops any conv whose
``messages.length === 0 && (_serverMsgCount||0) === 0 && !_needsLoad``.

The backend emits the count under DIFFERENT keys depending on which list shape
served the row:
  • ``lib/conversations/meta_cache.py`` (the ``?meta=1`` sidebar path) → ``messageCount``
  • ``routes/conversations.py::_conv_row_to_meta_dict`` (default list) → ``msgCount`` / ``msg_count``
  • the IDB cache meta → ``msgCount``

The shell builder historically read ONLY ``sc.messageCount``. Fed any payload
lacking that exact key (e.g. the ``msgCount``-only default shape), the shell got
``_serverMsgCount=0 && _needsLoad=false`` → invisible in the sidebar until the
user clicked it (which triggers a body load and rehydrates it). This is the
"interrupted conversations don't show after restart" symptom.

THE FIX
-------
A ``_serverConvCount(sc)`` helper coalesces ``messageCount || msgCount ||
msg_count`` at every shell-building site, so the visibility gate is robust to
which list shape served the row.

This drives the REAL shipped ``loadConversationsFromServer`` under node.

NEUTER: replace the coalescing read with the old ``sc.messageCount``-only read
in a COPY → a ``msgCount``-only row again yields an invisible shell, proving the
fix is load-bearing. Real file untouched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
CONV_JS = os.path.join(JS_DIR, 'core', 'conversations.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

let SERVER_LIST = [];

global.activeConvId = null;
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.conversations = [];
global.debugLog = () => {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.saveConversations = () => {};
global.loadConversationMessages = async () => {};
global.loadFolders = async () => {};
// Real settings applier is a no-op for these shells (no activeTaskId needed).
global._applySettingsToConv = () => {};
global._migratePinnedToFolder = () => {};
global._ensureMsgId = (m) => m;
global.ConvCache = { put() {}, remove() {}, get: async () => null };
global._bootLoadInFlight = false;
// The shipped loader fetches via Api.conversations.listMeta (the ?meta=1
// sidebar seam) — stubbing the retired `.list` leaves listMeta undefined and
// the loader bails before creating any shell.
const _listResp = () => ({
  ok: true, status: 200,
  headers: { get: () => null },
  json: async () => SERVER_LIST,
});
global.Api = { conversations: { listMeta: async () => _listResp(),
                                list: async () => SERVER_LIST,
                                get: async () => null } };
global.fetch = async () => ({
  ok: true, status: 200,
  headers: { get: () => null },
  json: async () => SERVER_LIST,
});
global.apiUrl = (p) => p;

for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));  // bundle-order conv family via _conv_bundle_sources.conv_family_sources

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

/* The sidebar visibility gate, verbatim from renderConversationList
 * (conversation_list.js): a conv is shown iff this predicate is true. */
function visible(c) {
  return c.messages.length > 0 || (c._serverMsgCount || 0) > 0 || c._needsLoad;
}

const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

(async () => {
  if (typeof loadConversationsFromServer !== 'function') {
    console.log('FAIL fn_exposed loadConversationsFromServer missing'); return;
  }
  check('fn_exposed', true);

  // ══ 1. msgCount-only row (default list shape) → VISIBLE shell ══
  {
    global.conversations = [];
    SERVER_LIST = [{ id: 'crash1', title: 'interrupted', msgCount: 4, msg_count: 4,
                     updatedAt: 5000, createdAt: 900, settings: null, rev: 3 }];
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    const c = conversations.find(x => x.id === 'crash1');
    check('msgcount_only_shell_created', !!c);
    check('msgcount_only_serverMsgCount', c && c._serverMsgCount === 4);
    check('msgcount_only_needsLoad', c && c._needsLoad === true);
    check('msgcount_only_visible', c && visible(c));
  }

  // ══ 2. messageCount-only row (?meta=1 sidebar shape) → still VISIBLE ══
  {
    global.conversations = [];
    SERVER_LIST = [{ id: 'meta1', title: 'meta', messageCount: 2,
                     updatedAt: 5000, createdAt: 900, settings: null, rev: 1 }];
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    const c = conversations.find(x => x.id === 'meta1');
    check('messagecount_shell_visible', c && visible(c) && c._serverMsgCount === 2);
  }

  // ══ 3. genuinely empty conv (count 0 under every key) → NOT visible ══
  //   (a real 0-message conv must still be filtered — the fix must not make
  //    every shell visible, only rescue ones with a real count.)
  {
    global.conversations = [];
    SERVER_LIST = [{ id: 'empty1', title: 'empty', msgCount: 0, messageCount: 0,
                     updatedAt: 5000, createdAt: 900, settings: null, rev: 0 }];
    global._bootLoadInFlight = false;
    await loadConversationsFromServer(); await flush();
    const c = conversations.find(x => x.id === 'empty1');
    check('empty_conv_not_visible', c && !visible(c));
  }

  console.log(out.join('\n'));
})();
"""


def _run(js_path: str):
    # Unique per-invocation harness name so parallel xdist workers never
    # clobber a shared fixed-name file (the failure mode that made this suite
    # flake under -n auto).
    import tempfile
    fd, harness = tempfile.mkstemp(prefix='_sidebar_shell_count_harness_',
                                   suffix='.js', dir=HERE)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(_HARNESS)
        # Eval the WHOLE conv family via the drift-proof closure (see
        # _conv_bundle_sources.conv_family_sources); NEUTER copies ride the
        # override (the mutated file REPLACES conversations.js).
        sys.path.insert(0, HERE)
        from _conv_bundle_sources import conv_family_sources
        override = None
        if os.path.basename(js_path) != 'conversations.js':
            override = {'core/conversations.js': js_path}
        extra_js = conv_family_sources(override=override)
        return subprocess.run(['node', harness, *extra_js],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_sidebar_shell_count_keys():
    proc = _run(CONV_JS)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'sidebar shell count-key regressions:\n' + output
    for inv in ('msgcount_only_shell_created', 'msgcount_only_serverMsgCount',
                'msgcount_only_needsLoad', 'msgcount_only_visible',
                'messagecount_shell_visible', 'empty_conv_not_visible'):
        assert f'PASS {inv}' in output, f'expected {inv} to PASS:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_sidebar_shell_count_keys_neuter(tmp_path):
    """NEUTER: revert the new-shell count read to the old ``sc.messageCount``-only
    form in a COPY → a ``msgCount``-only row again builds an INVISIBLE shell
    (_serverMsgCount=0, _needsLoad=false), proving the coalescing fix is
    load-bearing. Real file untouched."""
    with open(CONV_JS, encoding='utf-8') as f:
        src = f.read()
    # Two sites read `const _scCount = _serverConvCount(sc);` — the harness
    # drives loadConversationsFromServer (the second). Anchor with the
    # following shell-build line so the neuter bites the path under test.
    anchor = ('const _scCount = _serverConvCount(sc);\n'
              '        const nc = {\n'
              '          id: sc.id,')
    assert anchor in src, 'new-shell count anchor not found — update the neuter target'
    neutered = src.replace(anchor, 'const _scCount = sc.messageCount || 0;\n'
                                   '        const nc = {\n'
                                   '          id: sc.id,', 1)
    assert neutered != src, 'neuter did not change source'
    nfile = tmp_path / 'conversations_neutered.js'
    nfile.write_text(neutered, encoding='utf-8')

    proc = _run(str(nfile))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed on neutered copy: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('msgcount_only_visible') is False, (
        'NEUTER did not bite: a msgCount-only row was still VISIBLE with the '
        'messageCount-only read — the test does not discriminate the fix.\n' + output)

    with open(CONV_JS, encoding='utf-8') as f:
        assert f.read() == src, 'shipped conversations.js was mutated by the neuter test'
