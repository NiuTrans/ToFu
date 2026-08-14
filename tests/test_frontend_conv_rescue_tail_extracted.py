"""Wire-parity guards for pt_3879f00e sub-part 2 slice 8 — extract the
pure ``_rescuableLocalTail`` verdict function from
static/js/core/conversations.js into a dedicated leaf
static/js/core/conv_rescue_tail.js.

`_rescuableLocalTail(localMsgs, serverMsgs)` is a pure, side-effect-free
verdict that answers ONE question: given the local and server message
arrays, which locally-held rows look persisted (carry an identity) but
are missing on the server, and thus MUST be pushed back rather than
overwritten? It is the guard that keeps a lost-race whole-blob write
from silently erasing already-committed autopilot appends.

It has exactly ONE call site (inside loadConversationMessages at ~L1453),
takes no DOM, no globals, no state — the ideal minimal seam. Moving it
to its own file:

  * makes the rule directly testable without spinning up the ~35k-char
    loadConversationMessages harness;
  * shrinks core/conversations.js by the ~25L pure function + banner;
  * costs nothing at runtime: it's a bare `function` in the same window
    scope after bundle concat, resolved at CALL TIME by the surviving
    caller inside conversations.js.

Failing-first: written BEFORE the extraction; each guard is RED until
the leaf lands and conversations.js delegates.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from _runtime_sections import runtime_section_names, runtime_section_path  # noqa: E402

CONV_JS = pathlib.Path(runtime_section_path('core/conversations.js'))
LEAF_JS = pathlib.Path(runtime_section_path('core/conv_rescue_tail.js'))
INDEX_HTML = ROOT / 'index.html'


# ---------------------------------------------------------------------------
# 1. Leaf module exists and defines _rescuableLocalTail at top-level
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_defines_rescuer_at_top_level():
    assert LEAF_JS.exists(), (
        f'{LEAF_JS} must exist — it houses the extracted '
        '_rescuableLocalTail verdict function from conversations.js')
    src = LEAF_JS.read_text()
    import re
    m = re.search(
        r'^function\s+_rescuableLocalTail\s*\(', src, re.MULTILINE)
    assert m, (
        '_rescuableLocalTail must be a top-level `function` in the '
        'leaf so bundle-concat exposes it via the shared window scope')


def test_leaf_carries_pivotal_body_lines():
    """The extracted body must preserve the load-bearing behaviour —
    NEUTER-detection for a stealth stub."""
    src = LEAF_JS.read_text()
    # Array shape guard — both inputs must be arrays or the verdict is empty
    assert 'Array.isArray(localMsgs)' in src, (
        '_rescuableLocalTail must guard Array.isArray(localMsgs)')
    assert 'Array.isArray(serverMsgs)' in src, (
        '_rescuableLocalTail must guard Array.isArray(serverMsgs)')
    # Length-comparison guard — never rescues when local ≤ server
    assert 'localMsgs.length <= serverMsgs.length' in src, (
        '_rescuableLocalTail must short-circuit when local is not '
        'longer than server')
    # Identity filter — only rows carrying _msgId or _isVirtualUser
    # are considered persisted-shape (a half-built optimistic draft has
    # no id, so it is NOT rescuable)
    assert '_msgId' in src, (
        '_rescuableLocalTail must gate rescue on _msgId identity')
    assert '_isVirtualUser' in src, (
        '_rescuableLocalTail must also honour _isVirtualUser identity')
    # Slice from serverMsgs.length onward (only the TAIL, not the whole
    # local array) — this is the specific rows the server is missing
    assert 'slice(serverMsgs.length)' in src, (
        '_rescuableLocalTail must slice localMsgs from serverMsgs.length '
        'onward — not the whole local array')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares _rescuableLocalTail inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_rescuer_inline():
    src = CONV_JS.read_text()
    import re
    m = re.search(
        r'^function\s+_rescuableLocalTail\s*\(', src, re.MULTILINE)
    assert m is None, (
        '_rescuableLocalTail must live in core/conv_rescue_tail.js, not '
        'inline in conversations.js')


def test_conversations_js_still_calls_rescuer():
    """The one CALL site inside loadConversationMessages must remain —
    otherwise the rescue rule is dead code and the whole-blob-overwrite
    data-loss regression re-opens."""
    src = CONV_JS.read_text()
    assert '_rescuableLocalTail(' in src, (
        'conversations.js must still CALL _rescuableLocalTail — the '
        'push-back guard inside loadConversationMessages depends on it')


# ---------------------------------------------------------------------------
# 3. Bundle manifest lists the leaf BEFORE conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_leaf_before_conversations_js():
    """Load order: leaf must precede conversations.js so the surviving
    call site inside loadConversationMessages resolves at CALL TIME via
    bundle-level window scope."""
    owners = runtime_section_names()
    idx_leaf = owners.index('core/conv_rescue_tail.js')
    idx_conv = owners.index('core/conversations.js')
    assert idx_leaf < idx_conv, (
        f'core/conv_rescue_tail.js (idx {idx_leaf}) must precede '
        f'core/conversations.js (idx {idx_conv})')


# ---------------------------------------------------------------------------
# 4. The page shell contains no raw app-script inventory
# ---------------------------------------------------------------------------
def test_index_html_has_no_raw_script_tag_for_leaf():
    src = INDEX_HTML.read_text()
    assert 'static/js/core/conv_rescue_tail.js' not in src
    assert '<!-- TOFU_APP_ASSETS -->' in src


# ---------------------------------------------------------------------------
# 5. Semantic check via node — the extracted rule still returns the
#    right verdict for the three canonical cases.
# ---------------------------------------------------------------------------
def test_rescuer_verdict_matrix_via_node():
    """Drive the extracted function through node to prove behaviour is
    byte-identical to the original inline body. Skipped when node is
    not on PATH (CI harness matches other frontend suites)."""
    import shutil
    import subprocess
    if shutil.which('node') is None:
        import pytest
        pytest.skip('node not on PATH — skipping semantic exercise')
    src = LEAF_JS.read_text()
    harness = src + '\n\n' + r'''
const assert = require('assert');

// Case 1: local shorter than server → no rescue
assert.deepStrictEqual(
  _rescuableLocalTail([{_msgId: 'a'}], [{_msgId: 'a'}, {_msgId: 'b'}]),
  []
);

// Case 2: local longer than server, tail carries _msgId → rescue those
assert.deepStrictEqual(
  _rescuableLocalTail(
    [{_msgId: 'a'}, {_msgId: 'b'}, {_msgId: 'c'}],
    [{_msgId: 'a'}]
  ),
  [{_msgId: 'b'}, {_msgId: 'c'}]
);

// Case 3: local longer than server, extra rows have NO identity → no rescue
assert.deepStrictEqual(
  _rescuableLocalTail(
    [{_msgId: 'a'}, {content: 'draft'}],
    [{_msgId: 'a'}]
  ),
  []
);

// Case 4: _isVirtualUser also counts as identity
assert.deepStrictEqual(
  _rescuableLocalTail(
    [{_msgId: 'a'}, {_isVirtualUser: true, content: 'vu-msg'}],
    [{_msgId: 'a'}]
  ),
  [{_isVirtualUser: true, content: 'vu-msg'}]
);

// Case 5: non-array input → empty
assert.deepStrictEqual(_rescuableLocalTail(null, []), []);
assert.deepStrictEqual(_rescuableLocalTail([], null), []);

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
