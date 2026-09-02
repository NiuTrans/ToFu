"""Regression harness for the conversation-list date-group collapse logic.

WHY
---
The sidebar groups conversations under collapsible date headers
(``_buildConvPlan`` in ``static/js/ui/conversation_list.js``). The ">30 days"
("older") bucket starts collapsed (``_collapsedConvGroups = new Set(['older'])``).

The bug this locks: when a user's conversations are ALL older than 30 days
AND there is no active conv (the welcome page, ``activeConvId === null``), the
sole ``older`` group stayed collapsed, so ``_buildConvPlan`` emitted ZERO
``.conv-item`` rows — only a collapsed header. The delegated click handler on
``#convList`` then had nothing to match (``e.target.closest('.conv-item')`` is
null), so clicking a conversation "did nothing". See JOURNAL 2026-06-27.

The fix force-expands a group that is the ONLY populated group, so there is
always something clickable. This test loads the REAL shipped JS under jsdom
(via the shared ``tests/_jsdom`` runner) and asserts:
  • all-old + no active conv  → rows ARE rendered (older force-expanded)
  • mixed recent+old          → older STAYS collapsed (default preserved)

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import runtime_section_path

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
    _isDebug: false,
    BASE_PATH: '',
    activeStreams: new Map(),
    pendingMessageQueue: new Map(),
    streamBufs: new Map(),
  },
});

const now = Date.now();
const DAY = 86400000;

function rowsAndOlder() {
  const list = document.getElementById('convList');
  const rows = list.querySelectorAll('.conv-item[data-conv-id]').length;
  const olderHdr = list.querySelector('.conv-date-group[data-group="older"]');
  const olderCollapsed = !!(olderHdr && olderHdr.classList.contains('collapsed'));
  return { rows, hasOlder: !!olderHdr, olderCollapsed };
}

// ── Scenario 1: ALL convs > 30 days old, welcome page (no active conv).
//    The sole "older" group must be force-expanded so rows render. ──
global.activeConvId = window.activeConvId = null;
global.conversations = window.conversations = [
  { id: 'old1', title: 'Old 1', _serverTurnCount: 1, updatedAt: now - 40 * DAY },
  { id: 'old2', title: 'Old 2', _serverTurnCount: 1, updatedAt: now - 55 * DAY },
  { id: 'old3', title: 'Old 3', _serverTurnCount: 1, updatedAt: now - 70 * DAY },
];
renderConversationList();
let r1 = rowsAndOlder();
check('allold_rows_rendered', r1.rows === 3);
check('allold_older_force_expanded', r1.hasOlder && r1.olderCollapsed === false);

// ── Scenario 2: mixed recent + old, welcome page. The "older" group is NOT
//    the sole populated group, so it must STAY collapsed (default preserved)
//    and only the recent row is clickable. ──
global.conversations = window.conversations = [
  { id: 'recent', title: 'Recent', _serverTurnCount: 1, updatedAt: now - 1000 },
  { id: 'old2', title: 'Old 2', _serverTurnCount: 1, updatedAt: now - 55 * DAY },
  { id: 'old3', title: 'Old 3', _serverTurnCount: 1, updatedAt: now - 70 * DAY },
];
window._lastConvListHash = '';   // force past the hash guard
renderConversationList();
let r2 = rowsAndOlder();
check('mixed_older_stays_collapsed', r2.hasOlder && r2.olderCollapsed === true);
check('mixed_only_recent_clickable', r2.rows === 1);

// ── Scenario 3: the reported bug — a group holding the ACTIVE conv ("today")
//    must still collapse when the user EXPLICITLY clicks it. The active-conv
//    force-expand guard must not override an explicit user toggle. ──
global.activeConvId = window.activeConvId = 'today1';
global.conversations = window.conversations = [
  { id: 'today1', title: 'Today 1', _serverTurnCount: 1, updatedAt: now - 1000 },
  { id: 'today2', title: 'Today 2', _serverTurnCount: 1, updatedAt: now - 2000 },
  { id: 'old3', title: 'Old 3', _serverTurnCount: 1, updatedAt: now - 70 * DAY },
];
window._lastConvListHash = '';
renderConversationList();
function todayState() {
  const list = document.getElementById('convList');
  const hdr = list.querySelector('.conv-date-group[data-group="today"]');
  const rows = list.querySelectorAll('.conv-item[data-conv-id]');
  const todayRows = [...rows].filter(r => r.dataset.convId === 'today1' || r.dataset.convId === 'today2').length;
  return { collapsed: !!(hdr && hdr.classList.contains('collapsed')), todayRows };
}
// Before the click: today is expanded (default) and its rows show.
let t0 = todayState();
check('today_expanded_before_toggle', t0.collapsed === false && t0.todayRows === 2);
// User clicks the "today" header → it must collapse despite holding the active conv.
_toggleConvGroup('today');
let t1 = todayState();
check('today_collapses_on_user_toggle', t1.collapsed === true && t1.todayRows === 0);

report();
"""


def test_conv_list_collapse_never_hides_all_rows():
    run_harness(
        target_js=runtime_section_path('ui/conversation_list.js'),
        body_js=_BODY,
        min_pass=6,
        label='conv-list collapse',
    )
