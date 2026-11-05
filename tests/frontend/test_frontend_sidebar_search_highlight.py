"""Sidebar search highlighting escapes text around and inside each match.

``highlightMatch`` (frontend/src/runtime/sections/ui/conversation_list.js)
used to run the query regex over already-escaped HTML, so searching "amp",
"lt", "quot" or "39" split entities such as ``&amp;`` with highlight markup,
and "<" or "&" never matched. It now matches the raw text and escapes every
piece, keeping the inserted markup fixed.
"""

from __future__ import annotations

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
    // Production escaping (frontend/src/html-safety.ts): quotes are entities
    // too, so "quot" and "39" searches can only split them if matched there.
    escapeHtml: (value) => (value ? String(value) : '').replace(/[&<>"']/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[character]),
    formatRelativeTime: () => '',
    _conversationDisplayTitle: (title, fallback) => title || fallback || '',
    conversationTimestampLabels: () => ({ date: '08/28', time: '10:00' }),
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

const hl = (text) => `<span class="sidebar-search-highlight">${text}</span>`;

check('entity_names_are_not_matched_inside_entities',
  highlightMatch('Tom & Jerry', 'amp') === escapeHtml('Tom & Jerry'));
check('quote_entity_is_not_split',
  highlightMatch('say "hi"', 'quot') === escapeHtml('say "hi"'));
check('apostrophe_entity_is_not_split',
  highlightMatch("it's", '39') === escapeHtml("it's"));
check('special_characters_match_their_raw_text',
  highlightMatch('a<b & c', '<') === `a${hl('&lt;')}b &amp; c`
  && highlightMatch('a<b & c', '&') === `a&lt;b ${hl('&amp;')} c`);
check('matches_keep_their_original_case',
  highlightMatch('Hello World, world', 'WORLD') === `Hello ${hl('World')}, ${hl('world')}`);
check('no_query_only_escapes',
  highlightMatch('<b>x</b>', '') === '&lt;b&gt;x&lt;/b&gt;');

const host = document.createElement('div');
host.innerHTML = highlightMatch('<img src=x onerror=alert(1)>', 'img');
check('markup_in_text_stays_text',
  host.querySelectorAll('img').length === 0
  && host.textContent === '<img src=x onerror=alert(1)>'
  && host.querySelectorAll('.sidebar-search-highlight').length === 1);

report();
"""


def test_sidebar_search_highlight_escapes_text_around_and_inside_matches():
    run_harness(
        target_js=runtime_section_path('ui/conversation_list.js'),
        body_js=_BODY,
        expect_pass=7,
        label='sidebar search highlight',
    )
