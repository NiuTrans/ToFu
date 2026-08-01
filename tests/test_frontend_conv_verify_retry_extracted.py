"""Wire-parity guards for pt_3879f00e sub-part 2 slice 11 — extract the
cache-verify self-heal retry cluster from static/js/core/conversations.js
into a dedicated leaf static/js/core/conv_verify_retry.js.

Cluster (~42 L, ONE bounded retry mechanism):

  * _CONV_VERIFY_RETRY_DELAYS_DEFAULT — const backoff schedule
  * _convVerifyRetryTimers            — active-timer map
  * _convVerifyRetryDelays()          — test-seam accessor with window override
  * _scheduleConvVerifyRetry(convId)  — the bounded self-heal driver

The cluster was deliberately left behind by slice 10 (which extracted
_setCacheVerifying + _openConvMayHoldOrphanGhost) because it depends on
_verifyActiveConvFromServer — a large still-unextracted function. Slice 11
demonstrates the cluster CAN move independently: it reaches
_verifyActiveConvFromServer at CALL time via bundle-level window scope,
identical to the pattern used by slices 5/6/9.

Extracting this cluster:
  * Isolates one bounded retry mechanism — an accidental unbounded retry
    is a hot production issue, and confining the retry cap + delays to
    one file makes the invariant reviewable.
  * Lets the retry contract be tested via a node harness driving the
    extracted function directly with mocked _verifyActiveConvFromServer,
    without touching the ~35k-char loadConversationMessages harness.
  * Shrinks core/conversations.js by ~42 L; the sub-epic passes -900 L
    cumulative.

The tests are failing-first: each guard is RED until the leaf lands +
conversations.js delegates via window scope + js_bundler manifest orders
it before conversations.js + index.html carries a dev-fallback tag.
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONV_JS = ROOT / 'static' / 'js' / 'core' / 'conversations.js'
LEAF_JS = ROOT / 'static' / 'js' / 'core' / 'conv_verify_retry.js'
INDEX_HTML = ROOT / 'index.html'


# ---------------------------------------------------------------------------
# 1. Leaf module exists and defines the two functions at top-level
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_defines_retry_functions_at_top_level():
    assert LEAF_JS.exists(), (
        f'{LEAF_JS} must exist — it houses the extracted cache-verify '
        'self-heal retry cluster from conversations.js')
    src = LEAF_JS.read_text()
    for fn_re, label in (
        (r'^function\s+_convVerifyRetryDelays\s*\(\s*\)\s*\{',
         '_convVerifyRetryDelays'),
        (r'^function\s+_scheduleConvVerifyRetry\s*\(\s*convId\s*\)\s*\{',
         '_scheduleConvVerifyRetry'),
    ):
        assert re.search(fn_re, src, re.MULTILINE), (
            f'{label} must be a top-level `function` in the leaf so '
            'bundle-concat exposes it via the shared window scope')


def test_leaf_carries_pivotal_body_lines():
    """The extracted body must preserve the load-bearing behaviour —
    NEUTER-detection for a stealth stub."""
    src = LEAF_JS.read_text()

    # The constant retry schedule (default 4s / 12s) — a bounded retry
    # is the whole point of this cluster.
    assert '_CONV_VERIFY_RETRY_DELAYS_DEFAULT' in src, (
        'The bounded default delay schedule constant must live in '
        'the leaf')
    assert '4000' in src and '12000' in src, (
        'The default 4s/12s backoff values must survive the extraction')

    # The active timer map — keyed lookup so a subsequent retry can
    # clearTimeout the previous scheduled attempt for the same conv.
    assert '_convVerifyRetryTimers' in src, (
        'The active-timer map must live in the leaf')

    # The bounded-retry cap: attempt >= delays.length is the halt.
    assert 'delays.length' in src, (
        'The bounded halt (attempt >= delays.length) must survive '
        'the extraction — an unbounded retry is a production hazard')

    # Only the OPEN conv self-heals in place.
    assert 'activeConvId' in src, (
        'The activeConvId guard (only OPEN conv self-heals) must '
        'survive the extraction — otherwise a background retry could '
        'race with the user opening a new conv')

    # The retry must reach _verifyActiveConvFromServer via bundle-level
    # window scope (typeof guard is the shape used by every prior slice
    # that reaches back into conversations.js).
    assert '_verifyActiveConvFromServer' in src, (
        'The bundle-scope call into _verifyActiveConvFromServer must '
        'survive — otherwise the cluster is dead code')
    assert "typeof _verifyActiveConvFromServer" in src, (
        'The typeof guard on _verifyActiveConvFromServer must survive '
        '— without it the leaf crashes when loaded before that fn')

    # The setTimeout + clearTimeout pair — the scheduling primitive.
    assert 'setTimeout' in src and 'clearTimeout' in src, (
        'The setTimeout/clearTimeout retry-scheduling primitives must '
        'survive the extraction')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares the cluster inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_cluster_inline():
    src = CONV_JS.read_text()
    for fn_re, label in (
        (r'^function\s+_convVerifyRetryDelays\s*\(', '_convVerifyRetryDelays'),
        (r'^function\s+_scheduleConvVerifyRetry\s*\(',
         '_scheduleConvVerifyRetry'),
        (r'^function\s+_clearConvVerifyRetryTimer\s*\(',
         '_clearConvVerifyRetryTimer'),
    ):
        assert re.search(fn_re, src, re.MULTILINE) is None, (
            f'{label} must live in core/conv_verify_retry.js, not '
            'inline in conversations.js')
    # The constant / map declarations must also be gone from conv.js —
    # the leaf owns them now.
    assert '_CONV_VERIFY_RETRY_DELAYS_DEFAULT' not in src, (
        'The delay-schedule constant must live in the leaf, not '
        'inline in conversations.js')
    # The active-timer map must not be referenced outside the leaf —
    # the eager verify-landed cleanup path (loadConversationMessages)
    # now delegates through _clearConvVerifyRetryTimer instead of
    # touching the map directly. This preserves encapsulation of the
    # bounded-retry primitives inside the leaf.
    assert '_convVerifyRetryTimers' not in src, (
        'The active-timer map must not be referenced from conversations.js '
        '— eager cleanup routes through _clearConvVerifyRetryTimer(convId) '
        'exposed by the leaf')


def test_leaf_exposes_clear_helper_for_eager_cleanup():
    """The eager verify-landed cleanup in loadConversationMessages
    used to poke the leaf's _convVerifyRetryTimers map directly; that
    path is now routed through a helper the leaf owns."""
    src = LEAF_JS.read_text()
    assert re.search(
        r'^function\s+_clearConvVerifyRetryTimer\s*\(\s*convId\s*\)\s*\{',
        src, re.MULTILINE), (
        '_clearConvVerifyRetryTimer must live in the leaf as a '
        'top-level function so conversations.js can call it via '
        'bundle-level window scope')
    # The helper must actually clear the timer + delete the entry —
    # a stealth stub would defeat the encapsulation.
    assert 'clearTimeout(_convVerifyRetryTimers[' in src, (
        '_clearConvVerifyRetryTimer must clearTimeout on the map entry')
    assert 'delete _convVerifyRetryTimers[' in src, (
        '_clearConvVerifyRetryTimer must delete the map entry')


def test_conversations_js_uses_leaf_helper_for_eager_cleanup():
    """conversations.js must invoke _clearConvVerifyRetryTimer at the
    verify-landed cleanup site — otherwise the retry can re-fire
    against a conv that just landed a fresh server payload."""
    src = CONV_JS.read_text()
    assert '_clearConvVerifyRetryTimer(convId)' in src, (
        'conversations.js must delegate the eager verify-landed cleanup '
        'through _clearConvVerifyRetryTimer(convId)')


def test_conversations_js_still_calls_scheduler():
    """The call sites (there are 3 inside loadConversationMessages / the
    apply-server-payload path) must remain — otherwise the retry never
    fires and a stale-cache conv never self-heals."""
    src = CONV_JS.read_text()
    # At least one caller must survive after extraction.
    assert '_scheduleConvVerifyRetry(' in src, (
        'conversations.js must still CALL _scheduleConvVerifyRetry — '
        'otherwise the bounded retry never fires and stale-cache '
        'convs never self-heal')


# ---------------------------------------------------------------------------
# 3. Bundle manifest lists the leaf BEFORE conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_leaf_before_conversations_js():
    """Load order: leaf must precede conversations.js so the caller
    sites inside loadConversationMessages resolve at CALL TIME via
    bundle-level window scope."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.js_bundler import _BUNDLE_FILES
    assert 'core/conv_verify_retry.js' in _BUNDLE_FILES, (
        'core/conv_verify_retry.js missing from _BUNDLE_FILES')
    idx_leaf = _BUNDLE_FILES.index('core/conv_verify_retry.js')
    idx_conv = _BUNDLE_FILES.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        f'core/conv_verify_retry.js (idx {idx_leaf}) must precede '
        f'core/conversations.js (idx {idx_conv})')


# ---------------------------------------------------------------------------
# 4. Dev-fallback <script> tag exists in index.html
# ---------------------------------------------------------------------------
def test_index_html_has_devfallback_script_tag_for_leaf():
    """Per the slice-4 silent-absence lesson: every _BUNDLE_FILES entry
    MUST have a matching <script> in index.html or the dev-fallback
    silently drops the leaf. Positioned BEFORE the conversations.js
    tag to mirror the manifest slot."""
    src = INDEX_HTML.read_text()
    assert 'core/conv_verify_retry.js' in src, (
        'index.html must have a <script defer src="static/js/core/'
        'conv_verify_retry.js"> tag for the dev fallback path')
    idx_leaf = src.index('core/conv_verify_retry.js')
    idx_conv = src.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        'core/conv_verify_retry.js <script> must appear BEFORE '
        'core/conversations.js in index.html for correct load order')


# ---------------------------------------------------------------------------
# 5. Semantic check via node — the extracted cluster still returns the
#    right verdict for the canonical retry cases.
# ---------------------------------------------------------------------------
def test_retry_cluster_verdict_matrix_via_node():
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
// Minimal shims for the module scope the leaf reads from at CALL time.
global.window = global;
let activeConvId = 'conv-x';
let conversations = [
  { id: 'conv-x', _verifyRetryCount: 0 },
];
const activeStreams = new Set();
let _editingMsgIdx = null;
let _setCacheVerifyingCalls = [];
function _setCacheVerifying(convId, on) {
  _setCacheVerifyingCalls.push([convId, on]);
}
let _verifyCalls = 0;
let _verifyResolvers = [];
function _verifyActiveConvFromServer(convId) {
  _verifyCalls += 1;
  return new Promise((resolve) => { _verifyResolvers.push(resolve); });
}

''' + src + r'''

const assert = require('assert');

// Case 1: default backoff is 4s / 12s.
const delays = _convVerifyRetryDelays();
assert.deepStrictEqual(delays, [4000, 12000], 'default delays');

// Case 2: window override wins.
global._CONV_VERIFY_RETRY_DELAYS = [10, 20, 30];
assert.deepStrictEqual(_convVerifyRetryDelays(), [10, 20, 30],
                       'window override delays');
delete global._CONV_VERIFY_RETRY_DELAYS;

// Case 3: bounded — attempt >= delays.length short-circuits.
conversations[0]._verifyRetryCount = 99;   // > delays.length
_verifyCalls = 0;
_scheduleConvVerifyRetry('conv-x');
// Wait a beat + verify no timer was armed
setTimeout(() => {
  assert.strictEqual(_verifyCalls, 0, 'no verify after cap exceeded');

  // Case 4: only OPEN conv self-heals; other-conv call is a no-op.
  conversations[0]._verifyRetryCount = 0;
  _verifyCalls = 0;
  _scheduleConvVerifyRetry('conv-other');
  setTimeout(() => {
    assert.strictEqual(_verifyCalls, 0, 'other-conv is a no-op');

    // Case 5: real fire — override delays to 5ms + drive one attempt.
    global._CONV_VERIFY_RETRY_DELAYS = [5];
    conversations[0]._verifyRetryCount = 0;
    _verifyCalls = 0;
    _scheduleConvVerifyRetry('conv-x');
    setTimeout(() => {
      assert.strictEqual(_verifyCalls, 1, 'one verify fired');
      assert.strictEqual(conversations[0]._verifyRetryCount, 1,
                         'attempt count incremented');

      console.log('OK');
    }, 20);
  }, 20);
}, 20);
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
