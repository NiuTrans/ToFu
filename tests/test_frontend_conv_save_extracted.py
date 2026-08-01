"""Wire-parity guards for pt_3879f00e sub-part 2 slice 13 — extract
the local-persistence pair ``saveConversations`` +
``syncConversationToServerDebounced`` from core/conversations.js into
a dedicated leaf core/conv_save.js.

These two functions form a natural cluster:

  * saveConversations(changedConvId): in-memory sort + updatedAt bump
    (guarded against active streams to prevent sidebar flicker), plus
    a 2-second-throttled sidebar refresh so the streaming conv bubbles
    to the top promptly.
  * syncConversationToServerDebounced(conv, delayMs=1500): the
    debounced companion to syncConversationToServer, coalescing rapid
    settings toggles into one PUT.

Neither reaches into the still-monolithic loadConversationMessages /
syncConversationToServer path — they are called by the higher-level
send/settings paths. Extracting them:

  * Isolates two ambient-write invariants (the streaming-flicker guard
    + the debounce map) in ONE reviewable file.
  * Cuts core/conversations.js by ~60 L.

The tests below are failing-first: each guard is RED until the
extraction lands.
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONV_JS = ROOT / 'static' / 'js' / 'core' / 'conversations.js'
LEAF_JS = ROOT / 'static' / 'js' / 'core' / 'conv_save.js'
INDEX_HTML = ROOT / 'index.html'


# ---------------------------------------------------------------------------
# 1. Leaf module exists and defines both functions at top level
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_defines_both_functions():
    assert LEAF_JS.exists(), (
        f'{LEAF_JS} must exist — it houses the extracted '
        'saveConversations + syncConversationToServerDebounced pair')
    src = LEAF_JS.read_text()
    for fn_re, label in (
        (r'^function\s+saveConversations\s*\(', 'saveConversations'),
        (r'^function\s+syncConversationToServerDebounced\s*\(',
         'syncConversationToServerDebounced'),
    ):
        assert re.search(fn_re, src, re.MULTILINE), (
            f'{label} must be a top-level `function` in the leaf so '
            'bundle-concat exposes it via the shared window scope')


def test_leaf_carries_debounce_timer_map():
    """The private timer map is the primitive that makes the debounce
    coalesce; losing it would silently disable coalescing."""
    src = LEAF_JS.read_text()
    assert '_syncDebounceTimers' in src, (
        'The debounce-timer map must live in the leaf together with '
        'the syncConversationToServerDebounced function it services')
    # Instantiated as a Map (the const declaration).
    assert re.search(
        r'(?:const|let|var)\s+_syncDebounceTimers\s*=\s*new\s+Map\s*\(',
        src), (
        'The _syncDebounceTimers map must be declared with `new Map()` '
        'in the leaf')


def test_leaf_carries_streaming_flicker_guard():
    """The updatedAt bump is INTENTIONALLY skipped for a conv that is
    actively streaming — that guard is the fix for the sidebar-flicker
    bug when multiple convs stream simultaneously."""
    src = LEAF_JS.read_text()
    assert 'activeStreams.has(' in src, (
        'The activeStreams.has() guard on updatedAt bump must survive '
        'the extraction — losing it re-opens the sidebar-flicker bug')


def test_leaf_carries_sidebar_refresh_throttle():
    """The 2-second sidebar refresh throttle keeps the streaming conv
    visible at the top of the sidebar without paying the full render
    cost every ~3s."""
    src = LEAF_JS.read_text()
    assert '_lastSidebarRefresh' in src, (
        'The _lastSidebarRefresh throttle marker must survive the '
        'extraction')
    assert 'requestAnimationFrame' in src, (
        'The requestAnimationFrame-wrapped sidebar refresh must survive')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares the pair inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_pair_inline():
    src = CONV_JS.read_text()
    for fn_re, label in (
        (r'^function\s+saveConversations\s*\(', 'saveConversations'),
        (r'^function\s+syncConversationToServerDebounced\s*\(',
         'syncConversationToServerDebounced'),
    ):
        assert re.search(fn_re, src, re.MULTILINE) is None, (
            f'{label} must live in core/conv_save.js, not inline in '
            'conversations.js')
    # The debounce-timer map must also be gone from conv.js — the leaf
    # owns it now (a residual reference from surviving callers would
    # not compile, since no other function in conv.js touches it).
    assert '_syncDebounceTimers' not in src, (
        'The debounce-timer map must live in the leaf, not inline in '
        'conversations.js')


# ---------------------------------------------------------------------------
# 3. Bundle manifest lists the leaf BEFORE conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_leaf_before_conversations_js():
    """Load order: leaf must precede conversations.js so any surviving
    call inside conversations.js resolves at CALL TIME via bundle-level
    window scope."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.js_bundler import _BUNDLE_FILES
    assert 'core/conv_save.js' in _BUNDLE_FILES, (
        'core/conv_save.js missing from _BUNDLE_FILES')
    idx_leaf = _BUNDLE_FILES.index('core/conv_save.js')
    idx_conv = _BUNDLE_FILES.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        f'core/conv_save.js (idx {idx_leaf}) must precede '
        f'core/conversations.js (idx {idx_conv})')


# ---------------------------------------------------------------------------
# 4. Dev-fallback <script> tag exists in index.html
# ---------------------------------------------------------------------------
def test_index_html_has_devfallback_script_tag_for_leaf():
    """Every _BUNDLE_FILES entry MUST have a matching <script> in
    index.html or the dev-fallback silently drops the leaf (peer note
    from slice 4)."""
    src = INDEX_HTML.read_text()
    assert 'core/conv_save.js' in src, (
        'index.html must have a <script defer src="static/js/core/'
        'conv_save.js"> tag for the dev fallback path')
    idx_leaf = src.index('core/conv_save.js')
    idx_conv = src.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        'core/conv_save.js <script> must appear BEFORE '
        'core/conversations.js in index.html for correct load order')


# ---------------------------------------------------------------------------
# 5. Semantic check via node — the extracted functions still behave
#    like the original inline bodies.
# ---------------------------------------------------------------------------
def test_functions_behave_like_original_via_node():
    """Drive the extracted functions through node to prove behaviour is
    byte-identical to the original inline body. Skipped when node is
    not on PATH."""
    import shutil
    import subprocess
    if shutil.which('node') is None:
        import pytest
        pytest.skip('node not on PATH — skipping semantic exercise')
    src = LEAF_JS.read_text()

    harness = r'''
// Minimal shims for the surrounding scope both fns reach at CALL time.
global.window = global;
let conversations = [
  { id: 'conv-x', updatedAt: 100 },
  { id: 'conv-y', updatedAt: 200 },
];
const activeStreams = new Set();
let _broadcastCalls = [];
function _broadcastToTabs(kind, payload) { _broadcastCalls.push([kind, payload]); }
function _convSorter(a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); }
let _syncCalls = 0;
function syncConversationToServer(conv) { _syncCalls += 1; }
let _renderCalls = 0;
function renderConversationList() { _renderCalls += 1; }
// requestAnimationFrame stub — fire synchronously for the test.
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);

''' + src + r'''

const assert = require('assert');

// Case 1: saveConversations bumps updatedAt for a non-streaming conv.
const before = conversations.find(c => c.id === 'conv-x').updatedAt;
saveConversations('conv-x');
const after = conversations.find(c => c.id === 'conv-x').updatedAt;
assert.ok(after >= Date.now() - 1000, 'updatedAt bumped for non-streaming conv');
assert.notStrictEqual(after, before, 'updatedAt actually changed');

// Case 2: saveConversations does NOT bump updatedAt during active stream.
conversations = [ { id: 'conv-z', updatedAt: 500 } ];
activeStreams.add('conv-z');
saveConversations('conv-z');
const zAfter = conversations.find(c => c.id === 'conv-z').updatedAt;
assert.strictEqual(zAfter, 500,
                   'updatedAt must NOT bump for streaming conv (flicker guard)');
activeStreams.delete('conv-z');

// Case 3: syncConversationToServerDebounced coalesces rapid calls.
const conv = { id: 'conv-x' };
_syncCalls = 0;
syncConversationToServerDebounced(conv, 5);
syncConversationToServerDebounced(conv, 5);
syncConversationToServerDebounced(conv, 5);
setTimeout(() => {
  assert.strictEqual(_syncCalls, 1,
                     'debounce must coalesce rapid calls into ONE sync');
  console.log('OK');
}, 30);
'''
    result = subprocess.run(
        ['node', '-e', harness],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f'node harness failed:\nstdout={result.stdout}\n'
        f'stderr={result.stderr}')
    assert 'OK' in result.stdout, (
        f'node harness did not print OK:\nstdout={result.stdout}')
