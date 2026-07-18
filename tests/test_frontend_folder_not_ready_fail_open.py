"""Regression: the sidebar must FAIL OPEN when folders have not loaded yet.

WHY
---
``renderConversationList`` (static/js/ui/conversation_list.js) partitions convs
by folder. Before ``areFoldersLoaded()`` is true it cannot know which folder a
conv belongs to. The OLD behaviour hid every conv that carried a ``folderId``
from server settings (``filtered = all.filter(c => !c.folderId)``) to avoid a
brief flash in the uncategorized view. But if folders NEVER load (a folder-load
failure / slow request), those conversations become permanently INVISIBLE — a
real, server-present conversation vanishes from the sidebar. That is exactly the
"I keep losing conversations" symptom.

Fix: fail OPEN — when ``!foldersReady`` show every conversation
(``filtered = all``). A momentary flash of a foldered conv in the uncategorized
list is strictly better than dropping it; once folders load the normal branch
re-partitions correctly.

This test drives the REAL shipped ``renderConversationList`` under jsdom with
``areFoldersLoaded: () => false`` and a conv carrying a ``folderId``:
  • fail-open  → the foldered conv IS rendered.
  • NEUTER (restore the old ``filter(c => !c.folderId)``) → it is HIDDEN, proving
    the fix is load-bearing.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from tests._jsdom import JS_DIR, ROOT, node_deps_available, run_harness

pytestmark = pytest.mark.unit

_SRC = os.path.join(JS_DIR, 'ui', 'conversation_list.js')

# The exact fail-open line the fix introduced, and the pre-fix hide it replaced.
_FAIL_OPEN = 'filtered = all;\n    }'
_OLD_HIDE = 'filtered = all.filter(c => !c.folderId);\n    }'

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="convList"></div><div id="sidebarSearchStats"></div></body>',
  targets: [process.argv[2]],
  globals: {
    formatRelativeTime: () => '',
    highlightMatch: (s) => s,
    sidebarSearchQuery: '',
    getFolders: () => [],            // no folders known yet
    getActiveFolderId: () => null,
    setActiveFolderId: () => {},
    areFoldersLoaded: () => false,   // ★ folders NOT ready — the failure case
    renderFolderTabs: () => {},
    _isDebug: false,
    BASE_PATH: '',
    activeStreams: new Map(),
    pendingMessageQueue: new Map(),
    streamBufs: new Map(),
  },
});

const now = Date.now();

// A real, server-present conversation that carries a folderId from settings,
// while folders have NOT loaded yet.
global.activeConvId = window.activeConvId = null;
global.conversations = window.conversations = [
  { id: 'foldered', title: 'In a folder', messages: [{ role: 'user' }],
    folderId: 'fold-1', updatedAt: now - 1000 },
  { id: 'plain', title: 'No folder', messages: [{ role: 'user' }],
    updatedAt: now - 2000 },
];
window._lastConvListHash = '';
renderConversationList();

const list = document.getElementById('convList');
const has = (id) => !!list.querySelector('.conv-item[data-conv-id="' + id + '"]');

// The env var NEUTER (set by the Python side) tells the harness which
// expectation to assert against the target it was handed.
if (process.env.NEUTER === '1') {
  // Old hide restored → the foldered conv must be GONE (bug reproduced).
  check('neuter_foldered_hidden', has('foldered') === false);
  check('neuter_plain_still_shown', has('plain') === true);
} else {
  // Fix → both convs render even though folders aren't ready.
  check('failopen_foldered_shown', has('foldered') === true);
  check('failopen_plain_shown', has('plain') === true);
}

report();
"""


def test_folder_not_ready_fails_open():
    """With folders not ready, a foldered conv is still rendered (fail open)."""
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    assert _FAIL_OPEN in src, (
        'fail-open marker not found — did the folders-not-ready branch change?')
    run_harness(
        target_js=_SRC,
        body_js=_BODY,
        min_pass=2,
        label='folder fail-open',
    )


def test_NC_old_hide_reproduces_lost_conv():
    """NEUTER: restore the pre-fix ``filter(c => !c.folderId)`` → the foldered
    conv disappears, proving the fail-open is load-bearing (not vacuous)."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed')
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    assert src.count(_FAIL_OPEN) == 1, 'expected exactly one fail-open site to neuter'
    neutered = src.replace(_FAIL_OPEN, _OLD_HIDE, 1)
    assert neutered != src

    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=os.path.dirname(_SRC), delete=False, encoding='utf-8'
    ) as fh:
        neutered_path = fh.name
        fh.write(neutered)
    try:
        os.environ['NEUTER'] = '1'
        run_harness(
            target_js=neutered_path,
            body_js=_BODY,
            min_pass=2,
            label='folder fail-open NC',
        )
    finally:
        os.environ.pop('NEUTER', None)
        try:
            os.remove(neutered_path)
        except OSError:
            pass
