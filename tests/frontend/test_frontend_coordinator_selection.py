"""The Coordinator row's late identity cannot erase sidebar selection."""
from __future__ import annotations

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="projectCoordinatorEntry"></div><div id="convList"></div><div id="sidebarSearchStats"></div></body>',
  targets: [process.argv[2]],
  globals: {
    formatRelativeTime: () => '',
    highlightMatch: (s) => s,
    _conversationDisplayTitle: (title, fallback) => title || fallback || '',
    conversationTimestampLabels: () => ({ date: 'today', time: '12:00' }),
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
global.activeConvId = window.activeConvId = 'ordinary';
global.conversations = window.conversations = [
  { id: 'leader', title: 'Coordinator', _serverTurnCount: 1, updatedAt: now },
  { id: 'ordinary', title: 'Ordinary', _serverTurnCount: 1, updatedAt: now - 1000 },
];
const entry = document.getElementById('projectCoordinatorEntry');
renderConversationList();
check('selection_initial', document.querySelector('[data-conv-id="ordinary"]').classList.contains('active'));
check('guard_untouched', true);
entry.innerHTML = '<section class="coordination-leader" data-coordinator-id="leader"></section>';
renderConversationList();
const selected = document.querySelector('[data-conv-id="ordinary"]');
check('selection_after_identity', selected.classList.contains('active'));
const originalRemove = selected.classList.remove.bind(selected.classList);
selected.classList.remove = (...tokens) => { if (tokens.includes('active')) check('no_active_removal', false); originalRemove(...tokens); };
conversations[0].updatedAt = now + 1000;
renderConversationList();
check('selection_after_stream_reorder', selected.classList.contains('active'));
entry.replaceChildren();
renderConversationList();
check('selection_after_identity_clears', document.querySelector('[data-conv-id="ordinary"]').classList.contains('active'));
report();
"""


def test_coordinator_identity_preserves_selected_row():
    run_harness(
        target_js=runtime_section_path('ui/conversation_list.js'),
        body_js=_BODY,
        min_pass=5,
        label='Coordinator identity and sidebar selection',
    )
