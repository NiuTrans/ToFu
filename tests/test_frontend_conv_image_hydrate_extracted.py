#!/usr/bin/env python3
"""Wire-parity for pt_3879f00e sub-part 2 slice 4 — image base64 hydrator.

Scope: ``_hydrateImageBase64(conv)`` (~58 lines at conversations.js L64) —
a pure per-conversation image-hydration helper that walks conv.messages,
fetches base64 for any msg.images entry that has a url but no base64
(post-restart images arrive from DB with base64 stripped), and stashes a
promise on ``conv._hydratePromise`` for downstream awaits.

Two CALL-time consumers inside conversations.js pass the SAME conv object
in from their surrounding hydration path:
  - loadConversationMessages inside the initial-hydration branch (L1240)
  - loadConversationMessages after the head+tail refresh path (L1832)

Extract to ``static/js/core/conv_image_hydrate.js`` (loaded via
``lib/js_bundler.py::_BUNDLE_FILES`` BEFORE ``core/conversations.js`` so
its two remaining call sites still resolve the bare name at runtime via
bundle-level window scope, matching the pattern established by slices 1-3).

Failing-first — this test asserts (RED before extraction, GREEN after):
  1. New leaf file ``static/js/core/conv_image_hydrate.js`` exists
     and defines ``_hydrateImageBase64`` at the file top level.
  2. ``lib/js_bundler.py::_BUNDLE_FILES`` lists the new file BEFORE
     ``core/conversations.js`` (bundle concat order gate).
  3. The original inline function body pivots (the ``for (const img of
     msg.images)`` loop, the ``FileReader`` + ``readAsDataURL`` chain, the
     ``conv._hydratePromise = Promise.all(promises)`` assignment) are all
     GONE from conversations.js — the function definition is gone from
     the file's global scope. Two CALL sites remain intact (bare
     ``_hydrateImageBase64(conv)``).
  4. The leaf file exposes the function to the bundle-level scope by
     defining it as a top-level ``function`` declaration (no IIFE / no
     module wrapper — the bundler concatenates files, not modules).
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEAF = os.path.join(_ROOT, 'static/js/core/conv_image_hydrate.js')
_CONV = os.path.join(_ROOT, 'static/js/core/conversations.js')
_BUNDLER = os.path.join(_ROOT, 'lib/js_bundler.py')


@_unit
def test_leaf_module_exists_and_defines_hydrator_at_top_level():
    """Slice 4: static/js/core/conv_image_hydrate.js exists and
    declares ``_hydrateImageBase64`` at the file's top level (not
    inside an IIFE, not inside a nested function)."""
    assert os.path.exists(_LEAF), (
        f'{_LEAF} missing — leaf file not created after slice 4'
    )
    with open(_LEAF, encoding='utf-8') as f:
        src = f.read()
    # Top-level ``function _hydrateImageBase64(`` — start-of-line anchored
    # so a wrapped definition (e.g. inside an IIFE at any indentation)
    # would not satisfy the guard.
    assert re.search(r'^function\s+_hydrateImageBase64\s*\(', src, re.M), (
        'conv_image_hydrate.js must declare _hydrateImageBase64 as a '
        'TOP-LEVEL function declaration (bundler concatenates files at '
        'file scope; a wrapped def would not leak into the shared window '
        'scope the two call sites rely on).'
    )


@_unit
def test_bundler_lists_leaf_before_conversations_js():
    """Slice 4: lib/js_bundler.py::_BUNDLE_FILES must list
    ``core/conv_image_hydrate.js`` BEFORE ``core/conversations.js``.

    Same load-order invariant as slices 1-3: the leaf loads first so its
    top-level function declaration lands in the shared bundle scope
    before conversations.js's two call sites execute (or, more precisely,
    before syncConversationToServer / loadConversationMessages are
    invoked — the callers themselves don't run at load time, but keeping
    the manifest order consistent with slices 1-3 avoids surprising a
    future reader who greps for the pattern).
    """
    with open(_BUNDLER, encoding='utf-8') as f:
        src = f.read()
    leaf_idx = src.find("'core/conv_image_hydrate.js'")
    conv_idx = src.find("'core/conversations.js'")
    assert leaf_idx != -1, (
        "lib/js_bundler.py::_BUNDLE_FILES must list "
        "'core/conv_image_hydrate.js' after slice 4"
    )
    assert conv_idx != -1, (
        "lib/js_bundler.py::_BUNDLE_FILES sanity: "
        "'core/conversations.js' entry missing"
    )
    assert leaf_idx < conv_idx, (
        f'conv_image_hydrate.js at pos {leaf_idx} must come BEFORE '
        f'conversations.js at pos {conv_idx} in _BUNDLE_FILES — same '
        f'load-order rule as slices 1-3.'
    )


@_unit
def test_conversations_js_no_longer_declares_hydrator_inline():
    """Slice 4: conversations.js MUST NOT re-carry the inline
    ``function _hydrateImageBase64(`` declaration. Its two CALL sites
    (bare ``_hydrateImageBase64(conv)``) MAY stay.

    A silent revert would put the declaration back — this gate is
    a fast tripwire against exactly that.
    """
    with open(_CONV, encoding='utf-8') as f:
        src = f.read()
    # No inline ``function _hydrateImageBase64(`` at ANY indentation.
    # A re-carry (silent revert) would put back the definition; a bare
    # call site ``_hydrateImageBase64(conv)`` is what remains.
    assert not re.search(r'\bfunction\s+_hydrateImageBase64\s*\(', src), (
        'conversations.js must NOT re-carry inline '
        '``function _hydrateImageBase64(...)`` — extracted to '
        'core/conv_image_hydrate.js in slice 4.'
    )
    # But the CALL sites MUST still exist (extraction is a move, not a
    # deletion — the callers keep working via the bundle-level scope).
    assert '_hydrateImageBase64(conv)' in src, (
        'conversations.js must still CALL _hydrateImageBase64(conv) at '
        'its two call sites — extraction is a move, not a removal of '
        'the hydration behaviour.'
    )


@_unit
def test_leaf_module_carries_the_pivotal_body_lines():
    """Slice 4: the extracted body's pivot lines live in the leaf.

    Anchors the FileReader chain + the promise-stash assignment to the
    leaf, so a future edit that leaves ``function`` header behind but
    hollows out the body flips this test.
    """
    with open(_LEAF, encoding='utf-8') as f:
        src = f.read()
    for pivot in (
        # FileReader → dataURL conversion is the core hydration act
        'FileReader',
        'readAsDataURL',
        # promise stash on conv — how upstream awaits the hydration
        'conv._hydratePromise',
        # msg.images loop — the traversal that decides what to fetch
        'msg.images',
    ):
        assert pivot in src, (
            f'conv_image_hydrate.js missing pivot {pivot!r} — the '
            f'extracted body must carry the hydration logic verbatim; '
            f'a hollow move (function header only) is not a valid slice.'
        )


if __name__ == '__main__':
    for fn in [
        test_leaf_module_exists_and_defines_hydrator_at_top_level,
        test_bundler_lists_leaf_before_conversations_js,
        test_conversations_js_no_longer_declares_hydrator_inline,
        test_leaf_module_carries_the_pivotal_body_lines,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
