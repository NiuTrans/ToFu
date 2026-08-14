"""Wire-parity guards for pt_3879f00e sub-part 2 slice 12 — extract the
``_mergeServerTranslations`` array-level wrapper from
static/js/core/conversations.js into the existing shared reducer
static/js/core/conv_reducers.js.

Family-completion move: ``conv_reducers.js`` already owns the two
per-message primitives (``_mergeTranslationFields`` — per-message
translated-content merge + ``_mergeTerminalTurnFields`` — per-message
terminal metadata merge, both extracted by slice 1). The array-level
wrapper (``_mergeServerTranslations``) has been living as a NESTED
closure inside ``loadConversationMessages`` (~L1130), called from THREE
sites inside the same 754L function.

Extracting it:
  * Promotes the array-level wrapper to the family's natural home — one
    reusable helper instead of a closure that could drift if a fourth
    consumer emerges.
  * Removes one closure declaration from ``loadConversationMessages``
    (shrinking a still-monolithic function) while keeping the three
    surviving call sites unchanged; they resolve at call time via the
    bundle-level ``window`` scope, identical to how
    ``_mergeTerminalTurnFields`` / ``_mergeTranslationFields`` already
    resolve there.
  * Adds a ``window._mergeServerTranslations = _mergeServerTranslations``
    export line so the cross-file resolution is unambiguous.

The tests below are failing-first: each guard is RED until the
extraction lands.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from _runtime_sections import runtime_section_names, runtime_section_path  # noqa: E402

CONV_JS = pathlib.Path(runtime_section_path('core/conversations.js'))
REDUCERS_JS = pathlib.Path(runtime_section_path('core/conv_reducers.js'))


# ---------------------------------------------------------------------------
# 1. conv_reducers.js declares the array-level wrapper at TOP level
# ---------------------------------------------------------------------------
def test_reducers_defines_merge_server_translations_at_top_level():
    src = REDUCERS_JS.read_text()
    assert re.search(
        r'^function\s+_mergeServerTranslations\s*\(\s*sourceMsgs\s*,\s*'
        r'destMsgs\s*\)\s*\{',
        src, re.MULTILINE), (
        '_mergeServerTranslations must be a top-level `function` in '
        'core/conv_reducers.js — matches _mergeTranslationFields / '
        '_mergeTerminalTurnFields siblings already living there')


def test_reducers_exposes_wrapper_on_window():
    """The peer-slice 1 pattern (window._mergeTranslationFields =
    ...) makes the symbol reachable from conversations.js's surviving
    call sites via bundle-level window scope. The new wrapper must
    follow the same convention."""
    src = REDUCERS_JS.read_text()
    assert 'runtimeScope._mergeServerTranslations = _mergeServerTranslations' in src, (
        'core/conv_reducers.js must expose _mergeServerTranslations on '
        'window — same convention slice 1 established for its two '
        'sibling primitives')


def test_reducers_wrapper_delegates_to_translation_fields_primitive():
    """The extracted wrapper must delegate to ``_mergeTranslationFields``
    (the per-message primitive that lives in the same file) — a stealth
    stub that hand-rolls the merge would drift from the primitive."""
    src = REDUCERS_JS.read_text()
    # Locate the wrapper's body and verify the primitive call site is
    # inside it (not merely elsewhere in the file).
    m = re.search(
        r'function\s+_mergeServerTranslations\s*\([^)]*\)\s*\{([\s\S]*?)\n\}',
        src, re.MULTILINE)
    assert m, '_mergeServerTranslations body not found in conv_reducers.js'
    body = m.group(1)
    assert '_mergeTranslationFields(' in body, (
        '_mergeServerTranslations must delegate per-message merges to '
        '_mergeTranslationFields — the primitive slice 1 extracted')


def test_reducers_wrapper_carries_pivotal_body_lines():
    """The extracted body must preserve the load-bearing behaviour —
    NEUTER-detection for a stealth stub."""
    src = REDUCERS_JS.read_text()
    m = re.search(
        r'function\s+_mergeServerTranslations\s*\([^)]*\)\s*\{([\s\S]*?)\n\}',
        src, re.MULTILINE)
    assert m, '_mergeServerTranslations body not found'
    body = m.group(1)
    # Both inputs must be arrays or the merge is a no-op returning 0.
    assert 'Array.isArray(sourceMsgs)' in body, (
        '_mergeServerTranslations must guard Array.isArray(sourceMsgs)')
    assert 'Array.isArray(destMsgs)' in body, (
        '_mergeServerTranslations must guard Array.isArray(destMsgs)')
    # Iterate over the overlap only (min of the two lengths).
    assert 'Math.min(sourceMsgs.length, destMsgs.length)' in body, (
        '_mergeServerTranslations must iterate over Math.min of the two '
        'lengths — a longer local tail is preserved unchanged')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares the closure inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_wrapper_inline():
    """The closure declaration inside loadConversationMessages must be
    gone — the surviving three call sites resolve via bundle-level
    window scope at call time."""
    src = CONV_JS.read_text()
    # The characteristic const _mergeServerTranslations = (...) => { ... }
    # closure form must be gone. A residual DOCSTRING comment mentioning
    # the symbol is permissible, but the closure body must not exist.
    assert not re.search(
        r'const\s+_mergeServerTranslations\s*=', src), (
        'The nested closure `const _mergeServerTranslations = ...` in '
        'loadConversationMessages must be gone — the extracted wrapper '
        'in conv_reducers.js is the single source of truth')
    # Function-form re-declaration is also forbidden — same principle.
    assert not re.search(
        r'^function\s+_mergeServerTranslations\s*\(', src, re.MULTILINE), (
        'conversations.js must not carry a top-level '
        '_mergeServerTranslations function — the extracted version '
        'lives in conv_reducers.js')


def test_conversations_js_still_calls_wrapper():
    """The three surviving call sites must remain — otherwise the
    on-open translation merge dies silently and the user sees English
    until the IDB cache expires."""
    src = CONV_JS.read_text()
    # Strip block+line comments so a docstring reference doesn't count.
    stripped = re.sub(r'/\*[\s\S]*?\*/', '', src)
    stripped = re.sub(r'//[^\n]*', '', stripped)
    n_calls = len(re.findall(
        r'_mergeServerTranslations\s*\(', stripped))
    assert n_calls >= 3, (
        f'conversations.js must still call _mergeServerTranslations at '
        f'~3 sites (found {n_calls}) — the surviving callers reach the '
        'extracted wrapper via bundle-level window scope')


# ---------------------------------------------------------------------------
# 3. Bundle load-order: conv_reducers.js already precedes conversations.js
# ---------------------------------------------------------------------------
def test_bundler_lists_reducers_before_conversations_js():
    """conv_reducers.js is already before conversations.js in the
    manifest (slice 1). This test double-checks: the promoted wrapper
    needs the same load order the primitives already have."""
    owners = runtime_section_names()
    idx_reducers = owners.index('core/conv_reducers.js')
    idx_conv = owners.index('core/conversations.js')
    assert idx_reducers < idx_conv, (
        f'core/conv_reducers.js (idx {idx_reducers}) must precede '
        f'core/conversations.js (idx {idx_conv}) so the extracted '
        '_mergeServerTranslations wrapper is on window before its '
        'surviving call sites fire')


# ---------------------------------------------------------------------------
# 4. Semantic check via node — the extracted wrapper still returns the
#    right verdict for the canonical merge cases.
# ---------------------------------------------------------------------------
def test_wrapper_merge_matrix_via_node():
    """Drive the extracted wrapper through node to prove behaviour is
    byte-identical to the original closure. Skipped when node is not
    on PATH."""
    import shutil
    import subprocess
    if shutil.which('node') is None:
        import pytest
        pytest.skip('node not on PATH — skipping semantic exercise')

    src = REDUCERS_JS.read_text()
    # The wrapper depends on _mergeTranslationFields, which is also in
    # conv_reducers.js. Load the WHOLE file so both symbols are in scope.
    harness = r'''
// Minimal browser surface — conv_reducers.js reads no DOM.
global.window = global;
global.debugLog = () => {};
// Some sibling functions call convTitleById / other globals; stub loosely.
global.conversations = [];
global.activeConvId = null;
global.T_UI = {};

''' + src + r'''

const assert = require('assert');

// Case 1: both arrays empty → 0 merges.
assert.strictEqual(_mergeServerTranslations([], []), 0);

// Case 2: non-array input → 0 merges (no throw).
assert.strictEqual(_mergeServerTranslations(null, [{content: 'x'}]), 0);
assert.strictEqual(_mergeServerTranslations([{content: 'x'}], null), 0);

// Case 3: overlap merges translated content from source into dest.
const dest = [
  { role: 'assistant', content: 'hi', _msgId: 'a' },
  { role: 'assistant', content: 'bye', _msgId: 'b' },
];
const source = [
  { role: 'assistant', content: 'hi', _msgId: 'a', translatedContent: '你好' },
  { role: 'assistant', content: 'bye', _msgId: 'b', translatedContent: '再见' },
];
const merged = _mergeServerTranslations(source, dest);
assert.strictEqual(merged, 2, 'both messages should merge');
assert.strictEqual(dest[0].translatedContent, '你好', 'dest[0] translation merged');
assert.strictEqual(dest[1].translatedContent, '再见', 'dest[1] translation merged');

// Case 4: dest longer than source — iterate only over the overlap
// (the extra dest[2] must be UNTOUCHED).
const dest2 = [
  { role: 'assistant', content: 'a', _msgId: 'a' },
  { role: 'assistant', content: 'b', _msgId: 'b' },
  { role: 'assistant', content: 'c', _msgId: 'c' },  // no source counterpart
];
const source2 = [
  { role: 'assistant', content: 'a', _msgId: 'a', translatedContent: 'A' },
  { role: 'assistant', content: 'b', _msgId: 'b', translatedContent: 'B' },
];
_mergeServerTranslations(source2, dest2);
assert.strictEqual(dest2[0].translatedContent, 'A');
assert.strictEqual(dest2[1].translatedContent, 'B');
assert.strictEqual(dest2[2].translatedContent, undefined,
                   'dest2[2] must be untouched — outside the overlap');

console.log('OK');
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
