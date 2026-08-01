"""Wire-parity guards for pt_3879f00e sub-part 2 slice 14 — extract
the per-message ``lightMsgs`` reducer from
static/js/core/conversations.js into the existing shared persist
helper module static/js/core/conv_persist_helpers.js as a top-level
``_lightMessageForSync`` function.

Family-completion move: ``conv_persist_helpers.js`` already owns
``_trimMsgForPersist`` (per-message transient-bloat stripper) that
this closure delegates to. The closure has been an inline arrow inside
syncConversationToServer (~L136) — extracting it:

  * Puts the per-message WIRE-shape reducer in its natural home
    alongside ``_trimMsgForPersist``.
  * Cuts syncConversationToServer's still-monolithic body.
  * Enables direct unit tests of the image-preview / pdfTexts /
    _pendingSync stripping without spinning up syncConversationToServer.

Failing-first: this test is written BEFORE the extraction. Each guard
turns RED until the extraction really happens and the closure is
replaced with `conv.messages.map(_lightMessageForSync)`.
"""

from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONV_JS = ROOT / 'static' / 'js' / 'core' / 'conversations.js'
HELPERS_JS = ROOT / 'static' / 'js' / 'core' / 'conv_persist_helpers.js'


# ---------------------------------------------------------------------------
# 1. conv_persist_helpers.js declares the per-message reducer at TOP level
# ---------------------------------------------------------------------------
def test_helpers_defines_light_message_reducer_at_top_level():
    src = HELPERS_JS.read_text()
    assert re.search(
        r'^function\s+_lightMessageForSync\s*\(\s*m\s*\)\s*\{',
        src, re.MULTILINE), (
        '_lightMessageForSync must be a top-level `function` in '
        'core/conv_persist_helpers.js — matches the _trimMsgForPersist '
        'sibling already living there')


def test_helpers_exposes_reducer_on_window():
    """The peer-slice 3 pattern (window._trimMsgForPersist = ...) makes
    the symbol reachable from conversations.js's surviving call via
    bundle-level window scope. The new reducer must follow suit."""
    src = HELPERS_JS.read_text()
    assert 'window._lightMessageForSync = _lightMessageForSync' in src, (
        'core/conv_persist_helpers.js must expose _lightMessageForSync '
        'on window — same convention slice 3 established for '
        '_trimMsgForPersist')


def test_helpers_reducer_delegates_to_trim_persist():
    """The extracted reducer must delegate to ``_trimMsgForPersist``
    (the transient-bloat stripper) — a stealth stub that skipped it
    would re-inflate DB payloads with usage._wire_fp diagnostics /
    done-round _partialOutput."""
    src = HELPERS_JS.read_text()
    m = re.search(
        r'function\s+_lightMessageForSync\s*\([^)]*\)\s*\{([\s\S]*?)\n\}',
        src, re.MULTILINE)
    assert m, '_lightMessageForSync body not found in conv_persist_helpers.js'
    body = m.group(1)
    assert '_trimMsgForPersist(' in body, (
        '_lightMessageForSync must delegate final transient-bloat '
        'stripping to _trimMsgForPersist — the sibling primitive')


def test_helpers_reducer_carries_pivotal_body_lines():
    """The extracted body must preserve the load-bearing behaviour —
    NEUTER-detection for a stealth stub."""
    src = HELPERS_JS.read_text()
    m = re.search(
        r'function\s+_lightMessageForSync\s*\([^)]*\)\s*\{([\s\S]*?)\n\}',
        src, re.MULTILINE)
    assert m
    body = m.group(1)
    # Image reduction: url path preserved, preview stripped to base64 head.
    assert 'images' in body, (
        '_lightMessageForSync must handle m.images reduction')
    assert 'preview' in body, (
        'The image preview truncation must survive the extraction')
    # pdfTexts reduction to a flat name/pages/textLength shape.
    assert 'pdfTexts' in body, (
        'pdfTexts reduction must survive the extraction')
    # _pendingSync clone-and-strip (client-only durability marker).
    assert '_pendingSync' in body, (
        'The _pendingSync clone-and-strip must survive — it MUST NEVER '
        'be persisted to the server')


# ---------------------------------------------------------------------------
# 2. conversations.js no longer declares the closure inline
# ---------------------------------------------------------------------------
def test_conversations_js_no_longer_declares_closure_inline():
    """The nested arrow closure `conv.messages.map((m) => { ... })`
    with the 50-line body must be gone. The surviving line calls the
    extracted reducer instead."""
    src = CONV_JS.read_text()
    # The characteristic multi-line arrow with `pdfTexts:` + `images:`
    # inside a `map((m) =>` body — that whole shape must be gone.
    m = re.search(
        r'conv\.messages\.map\s*\(\s*\(m\)\s*=>\s*\{[\s\S]{200,}?\}\s*\)',
        src)
    assert m is None, (
        'The 50-line per-message arrow closure in '
        'syncConversationToServer must be gone — the extracted '
        '_lightMessageForSync in conv_persist_helpers.js is the '
        'single source of truth')


def test_conversations_js_uses_extracted_reducer():
    """The surviving call must use the extracted symbol at the bare
    name (bundle-scope resolution)."""
    src = CONV_JS.read_text()
    assert '_lightMessageForSync' in src, (
        'conversations.js must call _lightMessageForSync — the '
        'extracted per-message wire-shape reducer')
    # And specifically it should appear inside the sync PUT
    # assembly, as .map(_lightMessageForSync).
    assert '.map(_lightMessageForSync)' in src, (
        'The surviving call in conversations.js must be the compact '
        '.map(_lightMessageForSync) shape')


# ---------------------------------------------------------------------------
# 3. Bundle load-order: conv_persist_helpers.js already precedes
#    conversations.js (established by slice 3).
# ---------------------------------------------------------------------------
def test_bundler_lists_helpers_before_conversations_js():
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.js_bundler import _BUNDLE_FILES
    assert 'core/conv_persist_helpers.js' in _BUNDLE_FILES
    idx_helpers = _BUNDLE_FILES.index('core/conv_persist_helpers.js')
    idx_conv = _BUNDLE_FILES.index('core/conversations.js')
    assert idx_helpers < idx_conv, (
        f'core/conv_persist_helpers.js (idx {idx_helpers}) must precede '
        f'core/conversations.js (idx {idx_conv}) so the extracted '
        '_lightMessageForSync is on window before its call fires')


# ---------------------------------------------------------------------------
# 4. Semantic check via node — the extracted reducer still returns the
#    right shape for the canonical message types.
# ---------------------------------------------------------------------------
def test_reducer_semantic_matrix_via_node():
    """Drive the extracted reducer through node to prove behaviour is
    byte-identical to the original inline closure. Skipped when node
    is not on PATH."""
    import shutil
    import subprocess
    if shutil.which('node') is None:
        import pytest
        pytest.skip('node not on PATH — skipping semantic exercise')

    src = HELPERS_JS.read_text()
    harness = r'''
// Minimal shims for the surrounding scope the reducer reaches at CALL time.
global.window = global;
global.apiUrl = (path) => 'http://server' + path;

''' + src + r'''

const assert = require('assert');

// Case 1: bare text message passes through cleanly.
const m1 = { role: 'user', content: 'hello' };
const r1 = _lightMessageForSync(m1);
assert.strictEqual(r1.content, 'hello');
assert.strictEqual(r1.role, 'user');
assert.strictEqual(r1.images, undefined);

// Case 2: image message — url preserved, preview goes through apiUrl.
const m2 = {
  role: 'user',
  content: '',
  images: [{ url: '/api/images/foo.png', mediaType: 'image/png', sizeKB: 42,
             preview: 'DELETED_BY_TEST' }],
};
const r2 = _lightMessageForSync(m2);
assert.strictEqual(r2.images[0].url, '/api/images/foo.png');
assert.strictEqual(r2.images[0].preview, 'http://server/api/images/foo.png');
assert.strictEqual(r2.images[0].mediaType, 'image/png');
assert.strictEqual(r2.images[0].sizeKB, 42);

// Case 3: image WITHOUT url — preview truncated at 200 + '...'.
const m3 = {
  role: 'user',
  content: '',
  images: [{ preview: 'x'.repeat(500), mediaType: 'image/png', sizeKB: 10 }],
};
const r3 = _lightMessageForSync(m3);
assert.strictEqual(r3.images[0].preview.length, 203, 'preview truncated at 200 + ...');
assert.ok(r3.images[0].preview.endsWith('...'), 'truncation marker present');

// Case 4: pdfTexts — shape flattened to name/pages/textLength/isScanned/method/text.
const m4 = {
  role: 'user',
  content: 'here is a PDF',
  pdfTexts: [{ name: 'doc.pdf', pages: 12, textLength: 4321,
               isScanned: false, method: 'text', text: 'HELLO' }],
};
const r4 = _lightMessageForSync(m4);
assert.strictEqual(r4.pdfTexts[0].name, 'doc.pdf');
assert.strictEqual(r4.pdfTexts[0].pages, 12);
assert.strictEqual(r4.pdfTexts[0].textLength, 4321);
assert.strictEqual(r4.pdfTexts[0].text, 'HELLO');

// Case 5: _pendingSync must be STRIPPED from the reduced copy
// (but NOT from the original — clone-and-strip contract).
const m5 = { role: 'user', content: 'x', _pendingSync: true };
const r5 = _lightMessageForSync(m5);
assert.strictEqual(r5._pendingSync, undefined,
                   '_pendingSync must be stripped from the reduced copy');
assert.strictEqual(m5._pendingSync, true,
                   'original message must NOT be mutated');

// Case 6: extra pdfPage/pdfTotal/pdfName/caption on an image are carried.
const m6 = {
  role: 'user',
  content: '',
  images: [{ url: '/api/images/bar.png', mediaType: 'image/png', sizeKB: 5,
             pdfPage: 3, pdfTotal: 10, pdfName: 'src.pdf',
             caption: 'diagram-2' }],
};
const r6 = _lightMessageForSync(m6);
assert.strictEqual(r6.images[0].pdfPage, 3);
assert.strictEqual(r6.images[0].pdfTotal, 10);
assert.strictEqual(r6.images[0].pdfName, 'src.pdf');
assert.strictEqual(r6.images[0].caption, 'diagram-2');

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
