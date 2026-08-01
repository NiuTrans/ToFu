"""Wire-parity guards for pt_3879f00e sub-part 2 slice 10 — extract the
cache-verify visibility pair (_setCacheVerifying + _openConvMayHoldOrphanGhost)
from static/js/core/conversations.js into a dedicated leaf
static/js/core/conv_verify_visibility.js.

The two functions form a small cohesive cluster: both live on the
cache-verification visibility path, both are pure (no state mutation
beyond a CSS class toggle), and both are called ONLY from inside
conversations.js (no cross-file callers). Moving them:

  * lets the ghost-shape rule be tested directly rather than through
    the ~750L loadConversationMessages harness;
  * shrinks the boot-critical conversations.js by ~36 L that never
    fires on open unless the cache-verify path activates;
  * keeps both symbols on window scope via bundle-concat, so the 11
    surviving call sites (9 for _setCacheVerifying, 2 for
    _openConvMayHoldOrphanGhost) resolve at CALL time as before.

Failing-first: these guards were written BEFORE the extraction; each is
RED until the leaf lands, the manifest is updated, and conversations.js
delegates.

The bounded self-heal retry cluster (_convVerifyRetryDelays,
_scheduleConvVerifyRetry) is DELIBERATELY LEFT IN CONVERSATIONS.JS —
it depends on the still-unextracted _verifyActiveConvFromServer path
and has deep test-seam plumbing exercised by an existing dedicated
suite. A future slice can extract it once that seam moves too.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONV_JS = ROOT / 'static' / 'js' / 'core' / 'conversations.js'
LEAF_JS = ROOT / 'static' / 'js' / 'core' / 'conv_verify_visibility.js'
INDEX_HTML = ROOT / 'index.html'


# ---------------------------------------------------------------------------
# 1. Leaf module exists and defines both functions at top-level
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_defines_pair_at_top_level():
    assert LEAF_JS.exists(), (
        f'{LEAF_JS} must exist — it houses the extracted cache-verify '
        'visibility pair from conversations.js')
    src = LEAF_JS.read_text()
    import re
    for sym in ('_setCacheVerifying', '_openConvMayHoldOrphanGhost'):
        m = re.search(
            r'^function\s+' + re.escape(sym) + r'\s*\(',
            src, re.MULTILINE,
        )
        assert m, (
            f'{sym} must be a top-level `function` in the leaf so '
            'bundle-concat exposes it via the shared window scope')


def test_leaf_carries_pivotal_body_lines():
    """The extracted bodies must preserve their load-bearing behaviour.
    Each anchor targets a distinct RULE that a stealth stub could break;
    NEUTER-detection for silent regressions."""
    src = LEAF_JS.read_text()

    # _setCacheVerifying: active-conv guard (never touches DOM for a
    # non-active conv — otherwise a background verify on a hidden tab
    # would flash-dim the visible viewport).
    assert 'convId !== activeConvId' in src, (
        '_setCacheVerifying must short-circuit when convId is not the '
        'active conv — otherwise a hidden-tab verify flashes the DOM')

    # _setCacheVerifying: typeof-guarded document access (allows node
    # harnesses to import the leaf without a DOM).
    assert "typeof document !== 'undefined'" in src, (
        '_setCacheVerifying must typeof-guard document access so node '
        'harnesses can drive it without a DOM')

    # _setCacheVerifying: targets the specific chat-viewport element
    # (chatInner) and toggles the exact class name that CSS rules match.
    assert "getElementById('chatInner')" in src, (
        '_setCacheVerifying must target #chatInner specifically — the '
        '.chat-cache-verifying dim style is scoped to that element')
    assert "'chat-cache-verifying'" in src, (
        '_setCacheVerifying must toggle the exact class name '
        '"chat-cache-verifying" — CSS matches this string')

    # _openConvMayHoldOrphanGhost: excludes live streams — a legitimate
    # "Preparing…" pre-first-token bubble must NEVER be flagged as a
    # ghost, otherwise the client would trigger a re-verify while the
    # stream is still going.
    assert 'activeStreams.has(convId)' in src, (
        '_openConvMayHoldOrphanGhost must exclude convs with a live '
        'stream — a pre-first-token bubble is not a ghost')

    # _openConvMayHoldOrphanGhost: tail-only shape check (not iterating
    # the whole array) — the ghost is by construction the LAST message,
    # a bubble left behind when its stream dropped.
    assert 'msgs[msgs.length - 1]' in src, (
        '_openConvMayHoldOrphanGhost must inspect only the tail message '
        '— ghosts are trailing-empty-assistant bubbles by construction')

    # _openConvMayHoldOrphanGhost: the FULL empty-shape signature.
    # Each of these fields being empty is required — a bubble with, say,
    # a finishReason is a completed one, not a ghost.
    for field in (
        "tail.role === 'assistant'",
        'tail.content',
        'tail.thinking',
        'tail.toolRounds',
        'tail.finishReason',
        'tail.error',
    ):
        assert field in src, (
            f'_openConvMayHoldOrphanGhost missing empty-shape check '
            f'for {field!r} — a ghost is empty on ALL of these axes')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares the pair inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_pair_inline():
    src = CONV_JS.read_text()
    import re
    for sym in ('_setCacheVerifying', '_openConvMayHoldOrphanGhost'):
        m = re.search(
            r'^function\s+' + re.escape(sym) + r'\s*\(',
            src, re.MULTILINE,
        )
        assert m is None, (
            f'{sym} must live in core/conv_verify_visibility.js, not '
            'inline in conversations.js')


def test_conversations_js_still_calls_the_pair():
    """The pair is called from ~11 sites inside conversations.js — if
    those calls disappear the cache-verify visibility feature dies
    silently. Assert bulk of call sites remain (allowing for surrounding
    refactors that may consolidate a couple)."""
    src = CONV_JS.read_text()
    # Strip block + line comments so we count REAL call sites.
    import re
    stripped = re.sub(r'/\*[\s\S]*?\*/', '', src)
    stripped = re.sub(r'//[^\n]*', '', stripped)
    n_set = len(re.findall(r'_setCacheVerifying\s*\(', stripped))
    n_ghost = len(re.findall(r'_openConvMayHoldOrphanGhost\s*\(', stripped))
    # Threshold lowered from 8 to 7 by slice 11 (2026-07-31): the
    # cache-verify self-heal retry cluster (_scheduleConvVerifyRetry +
    # friends) was extracted into core/conv_verify_retry.js, and its
    # ONE call to _setCacheVerifying(convId, false) went with it. The
    # cross-file resolution still works via bundle-level window scope,
    # so the visibility feature is intact — the count just reflects
    # what LIVES in conversations.js after this move.
    assert n_set >= 7, (
        f'conversations.js must still call _setCacheVerifying at ~8 sites '
        f'(found {n_set}) — extraction is a move, callers are not removed')
    assert n_ghost >= 2, (
        f'conversations.js must still call _openConvMayHoldOrphanGhost at '
        f'2 sites (found {n_ghost}) — extraction is a move')


# ---------------------------------------------------------------------------
# 3. Bundle manifest lists the leaf BEFORE conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_leaf_before_conversations_js():
    """Load order: leaf must precede conversations.js so the 11
    surviving bare-name call sites resolve at CALL time via bundle-level
    window scope."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.js_bundler import _BUNDLE_FILES
    assert 'core/conv_verify_visibility.js' in _BUNDLE_FILES, (
        'core/conv_verify_visibility.js missing from _BUNDLE_FILES')
    idx_leaf = _BUNDLE_FILES.index('core/conv_verify_visibility.js')
    idx_conv = _BUNDLE_FILES.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        f'core/conv_verify_visibility.js (idx {idx_leaf}) must precede '
        f'core/conversations.js (idx {idx_conv})')


# ---------------------------------------------------------------------------
# 4. Dev-fallback <script> tag exists in index.html
# ---------------------------------------------------------------------------
def test_index_html_has_devfallback_script_tag_for_leaf():
    """Per the peer's slice-4 note: every _BUNDLE_FILES entry MUST have
    a matching <script> in index.html or the bundling-failed fallback
    path silently drops the leaf."""
    src = INDEX_HTML.read_text()
    assert 'core/conv_verify_visibility.js' in src, (
        'index.html must have a <script defer src="static/js/core/'
        'conv_verify_visibility.js"> tag for the dev fallback path')
    idx_leaf = src.index('core/conv_verify_visibility.js')
    idx_conv = src.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        'core/conv_verify_visibility.js <script> must appear BEFORE '
        'core/conversations.js in index.html for correct load order '
        'on the dev-fallback path')


# ---------------------------------------------------------------------------
# 5. Semantic exercise via node — the two predicates return the right
#    verdicts for a canonical matrix of inputs.
# ---------------------------------------------------------------------------
def test_pair_verdict_matrix_via_node():
    """Drive both functions through node to prove behaviour is
    byte-identical to the original inline bodies. Skipped when node is
    not on PATH."""
    import shutil
    import subprocess
    if shutil.which('node') is None:
        pytest.skip('node not on PATH — skipping semantic exercise')
    leaf = LEAF_JS.read_text()
    # Provide bundle-scope stubs for the free names the pair reaches:
    # activeConvId, activeStreams, and a minimal DOM.
    harness = r'''
let activeConvId = 'conv-A';
let activeStreams = new Set();
let classListState = { has: {} };
globalThis.document = {
  getElementById: (id) => {
    if (id !== 'chatInner') return null;
    return {
      classList: {
        add: (cls) => { classListState.has[cls] = true; },
        remove: (cls) => { delete classListState.has[cls]; },
      },
    };
  },
};
const assert = require('assert');
''' + leaf + r'''

// ============ _setCacheVerifying =============
// (a) Toggling ON for the active conv adds the dim class.
classListState = { has: {} };
_setCacheVerifying('conv-A', true);
assert.strictEqual(classListState.has['chat-cache-verifying'], true,
  '(a) toggling ON active conv must add .chat-cache-verifying');

// (b) Toggling OFF for the active conv removes the dim class.
_setCacheVerifying('conv-A', false);
assert.strictEqual(classListState.has['chat-cache-verifying'], undefined,
  '(b) toggling OFF active conv must remove .chat-cache-verifying');

// (c) Toggling for a NON-active conv must NOT touch the DOM (would
//     flash-dim the visible viewport for a hidden-tab verify).
classListState = { has: {} };
_setCacheVerifying('conv-OTHER', true);
assert.strictEqual(classListState.has['chat-cache-verifying'], undefined,
  '(c) toggling for non-active conv MUST NOT touch the DOM');

// ============ _openConvMayHoldOrphanGhost =============
// (d) Live stream on the conv → not a ghost (a pre-first-token bubble
//     is legit).
activeStreams = new Set(['conv-live']);
const liveConv = { messages: [{role:'assistant'}] };
assert.strictEqual(_openConvMayHoldOrphanGhost(liveConv, 'conv-live'), false,
  '(d) live-streaming conv MUST NOT be flagged as ghost');

// (e) Empty conv → not a ghost (nothing to reconcile).
activeStreams = new Set();
assert.strictEqual(_openConvMayHoldOrphanGhost({ messages: [] }, 'x'), false,
  '(e) empty conv is not a ghost');

// (f) The specific ghost SHAPE: assistant tail / no content / no
//     thinking / no toolRounds / no finishReason / no error → GHOST.
const ghost = { messages: [
  {role:'user', content:'hi'},
  {role:'assistant'},
]};
assert.strictEqual(_openConvMayHoldOrphanGhost(ghost, 'x'), true,
  '(f) trailing empty-assistant with no fields IS a ghost');

// (g) Tail with finishReason → NOT a ghost (it completed).
const done = { messages: [{role:'assistant', finishReason:'stop'}] };
assert.strictEqual(_openConvMayHoldOrphanGhost(done, 'x'), false,
  '(g) tail with finishReason is a completed reply, not a ghost');

// (h) Tail with content → NOT a ghost.
const withText = { messages: [{role:'assistant', content:'hello'}] };
assert.strictEqual(_openConvMayHoldOrphanGhost(withText, 'x'), false,
  '(h) tail with content is not a ghost');

// (i) Tail with toolRounds → NOT a ghost (real tool activity).
const withTools = { messages: [{role:'assistant', toolRounds:[{}]}] };
assert.strictEqual(_openConvMayHoldOrphanGhost(withTools, 'x'), false,
  '(i) tail with toolRounds is not a ghost');

// (j) null conv → false (defensive).
assert.strictEqual(_openConvMayHoldOrphanGhost(null, 'x'), false,
  '(j) null conv → false');

console.log('OK');
'''
    result = subprocess.run(
        ['node', '-e', harness],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f'node harness failed:\nstdout={result.stdout}\nstderr={result.stderr}')
    assert 'OK' in result.stdout, (
        f'node harness did not print OK:\nstdout={result.stdout}')
