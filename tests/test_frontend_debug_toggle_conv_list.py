"""Regression guard: the debug-mode toggle must refresh the sidebar IMMEDIATELY —
both on Settings toggle AND on boot-time flags arrival.

WHY
---
The copy-conv-ID button is baked per-row into ``_buildConvItemHTML``
(``static/js/ui/conversation_list.js``) under ``_featureFlags.debug_mode`` at
HTML-build time. Two refresh triggers existed, and both were silently eaten by
``renderConversationList()``'s split-hash early-return (struct + status hashes
did not include the debug flag):

1. Settings toggle — ``settings/save_export.js`` calls
   ``renderConversationList()`` right after flipping the flag → no-op.
2. BOOT RACE — ``index.html``'s async ``loadFeatureFlags`` assigns
   ``_featureFlags`` AFTER the conv list may already have rendered (rows baked
   with debug=false) and previously never re-rendered the sidebar at all, so
   an always-debug user could STILL miss the button until an unrelated struct
   change or a full page load.

Fix: (a) fold ``DBG<0|1>`` into ``_structHash`` so any flag flip forces a full
rebuild through the normal path; (b) ``loadFeatureFlags`` now calls
``renderConversationList()`` once flags arrive (index.html).

Coverage:
  • jsdom (real conversation_list.js):
      1. boot with flags NOT yet arrived (``_featureFlags = {}``) → no buttons
      2. flags arrive (``debug_mode: true``) + the loadFeatureFlags-style
         re-render, WITHOUT touching hash caches → buttons appear
      3. Settings-toggle OFF → buttons disappear immediately
      4. NC: unrelated struct change (rename) rebuilds through the normal
         path — proves the assertions aren't an always-rerender artifact
  • static wire guard (index.html): the boot-time re-render line must exist
    inside loadFeatureFlags after ``_featureFlags = flags;`` — the jsdom
    harness cannot load index.html's inline script, so this pins the wiring
    against silent removal.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

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
    getFolders: () => [],
    getActiveFolderId: () => null,
    setActiveFolderId: () => {},
    areFoldersLoaded: () => true,
    renderFolderTabs: () => {},
    BASE_PATH: '',
    activeStreams: new Map(),
    pendingMessageQueue: new Map(),
    streamBufs: new Map(),
    _featureFlags: {},
  },
});

const now = Date.now();
global.activeConvId = window.activeConvId = null;
global.conversations = window.conversations = [
  { id: 'c1', title: 'Conv 1', messages: [{ role: 'user' }], updatedAt: now - 1000 },
  { id: 'c2', title: 'Conv 2', messages: [{ role: 'user' }], updatedAt: now - 2000 },
];

function copyIdCount() {
  return document.getElementById('convList').querySelectorAll('.conv-copy-id').length;
}

// ── Boot scenario: the conv list renders BEFORE the async /api/v1/features
//    response arrives (rows baked with debug=false). ──
// NOTE: the harness evals targets in Node GLOBAL scope and mirrors each
// injected global onto both `global` and `win` — so a REASSIGNMENT must hit
// both (mutating the shared object works with window-only assignment, but a
// fresh object literal does not). Mirrors index.html's `_featureFlags = flags`.
global._featureFlags = window._featureFlags = {};   // flags not yet arrived
renderConversationList();
check('boot_flags_pending_no_buttons', copyIdCount() === 0);

// Flags arrive. loadFeatureFlags assigns _featureFlags and then calls
// renderConversationList() (index.html) — simulated here with NO hash-cache
// reset, exactly like the real call. The DBG token flip must force a rebuild.
global._featureFlags = window._featureFlags = { debug_mode: true };
renderConversationList();
check('boot_flags_arrival_rebuilds_sidebar', copyIdCount() === 2);

// ── Settings-toggle scenario: flip OFF mid-session → buttons disappear
//    immediately (same renderConversationList path saveSettings uses). ──
window._featureFlags.debug_mode = false;
renderConversationList();
check('toggle_off_buttons_removed_immediately', copyIdCount() === 0);

// Flip back ON — re-appears, still no reload / no hash reset.
window._featureFlags.debug_mode = true;
renderConversationList();
check('toggle_on_buttons_appear_immediately', copyIdCount() === 2);

// ── NC: an unrelated struct change (rename) must still rebuild rows through
//    the normal path — proves the harness exercises real rebuilds, not some
//    always-rerender artifact that would make the checks above vacuous. ──
conversations[0].title = 'Conv 1 renamed';
renderConversationList();
check('nc_struct_change_rebuilds_with_flag', copyIdCount() === 2);

report();
"""


def test_debug_toggle_and_boot_flags_refresh_conv_list_immediately():
    run_harness(
        target_js=os.path.join(JS_DIR, 'ui', 'conversation_list.js'),
        body_js=_BODY,
        min_pass=5,
        label='debug toggle + boot flags → conv list immediate refresh',
    )


def test_boot_load_feature_flags_rerenders_conv_list_wiring():
    """Static wire guard: index.html's loadFeatureFlags must re-render BOTH
    debug-mode consumers after _featureFlags arrives (boot-race half of the
    fix): the sidebar (renderConversationList) AND the message area
    (ConvView.replaceAll — trace_id / cost-popover debug rows are baked into
    message HTML under _featureFlags.debug_mode by finish_info.js). The jsdom
    harness cannot execute index.html's inline script, so pin the wiring here.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'index.html'), encoding='utf-8') as fh:
        html = fh.read()
    m = re.search(
        r'loadFeatureFlags\(\)\s*\{(?P<body>.*?)\}\s*\)\(\);',
        html,
        re.DOTALL,
    )
    assert m, 'loadFeatureFlags IIFE not found in index.html'
    body = m.group('body')
    assign = body.find('_featureFlags = flags;')
    assert assign != -1, 'loadFeatureFlags no longer assigns _featureFlags'
    rerender = body.find('renderConversationList()', assign)
    assert rerender != -1, (
        'loadFeatureFlags assigns _featureFlags but never calls '
        'renderConversationList() — boot-time debug users lose the '
        'copy-ID button again (race: list rendered before flags arrived)'
    )
    repaint = body.find('ConvView.replaceAll', assign)
    assert repaint != -1, (
        'loadFeatureFlags re-renders the sidebar but never repaints the '
        'message area (ConvView.replaceAll) — boot-time debug users lose '
        'trace_id / cost-debug rows baked by finish_info.js (same race)'
    )
    # Boot repaint must not yank the user to the bottom of the conversation.
    scroll_opt = body.find('forceScroll', repaint - 400)
    assert scroll_opt != -1 and 'forceScroll: false' in body[repaint - 400:repaint + 200], (
        'ConvView.replaceAll in loadFeatureFlags must pass forceScroll: false '
        '(boot-time repaint must preserve scroll position)'
    )
