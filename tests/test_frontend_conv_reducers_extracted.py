#!/usr/bin/env python3
"""Wire-parity for pt_3879f00e sub-part 2 — core/conversations.js decompose.

Scope: pull the 5 pure reducers at the top of core/conversations.js
(convAutoTranslate / assistantTailIsPriorTurn /
pollWriteWouldClobberSettledTail / convTitleById /
convAutoTranslateEffective) — ~166 lines of stateless helpers — into a
new leaf module ``static/js/core/conv_reducers.js`` so:

  (1) conversations.js shrinks by ~166 lines (2461 → ~2295), attacking
      the 134 KB core-bundle heavyweight the Epic-E dispatch names.
  (2) The reducers become independently discoverable / testable, and
      any future test can eval-load THIS one file (not the whole
      2461-line monolith with its Api / conversations / autoTranslate
      globals) to unit-test them.
  (3) The bundle order guarantees the leaf loads BEFORE conversations.js
      so its downstream consumers still resolve the bare names at
      runtime.

Failing-first: asserts (RED before extraction, GREEN after):

  1. ``static/js/core/conv_reducers.js`` exists and declares all 5
     reducers (via ``function convAutoTranslate`` etc.), and exposes
     them on ``window.*``.
  2. ``static/js/core/conversations.js`` NO LONGER declares those 5
     reducers (search for ``function convAutoTranslate(`` etc. — the
     definition site, not comment references).
  3. ``lib/js_bundler.py::_BUNDLE_FILES`` lists
     ``core/conv_reducers.js`` BEFORE ``core/conversations.js`` so the
     concatenated bundle preserves the load order (any consumer
     inside conversations.js that reads a reducer at IIFE time —
     currently none, but this locks future edits).
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from tests._runtime_sections import runtime_section, runtime_section_names


_REDUCERS = (
    'convAutoTranslate',
    'assistantTailIsPriorTurn',
    'pollWriteWouldClobberSettledTail',
    'convTitleById',
    'convAutoTranslateEffective',
)


def _read(rel_path: str) -> str:
    return runtime_section(rel_path.removeprefix('static/js/'))


@_unit
def test_conv_reducers_leaf_module_exists_and_declares_five():
    """Slice: the new leaf module ``static/js/core/conv_reducers.js``
    must exist and declare each of the 5 reducers via ``function <name>(``
    (the definition site), plus expose each on ``window.*``."""
    src = _read('static/js/core/conv_reducers.js')
    for name in _REDUCERS:
        # Definition site: `function <name>(` (allow `async function` too, though
        # these are all sync).
        assert re.search(rf'\bfunction\s+{re.escape(name)}\s*\(', src), (
            f'static/js/core/conv_reducers.js must define {name} '
            f'via `function {name}(...)` — extraction incomplete'
        )
        assert f'runtimeScope.{name} = {name}' in src, (
            f'static/js/core/conv_reducers.js must expose window.{name} '
            f'= {name} so downstream typeof-guarded reads still resolve'
        )


@_unit
def test_conversations_js_no_longer_declares_the_five_reducers():
    """Slice: ``static/js/core/conversations.js`` MUST NOT still carry
    a `function <name>(` definition for any of the 5 reducers — the
    extraction is only real when the OLD site is gone (otherwise both
    files declare it and the last one wins, silently)."""
    src = _read('static/js/core/conversations.js')
    for name in _REDUCERS:
        m = re.search(rf'\bfunction\s+{re.escape(name)}\s*\(', src)
        assert not m, (
            f'static/js/core/conversations.js must NOT still declare '
            f'`function {name}(...)` — leftover definition would '
            f'shadow the extracted leaf'
        )


@_unit
def test_bundle_manifest_loads_reducers_before_conversations():
    """Slice: lib/js_bundler.py's ``_BUNDLE_FILES`` must list
    ``core/conv_reducers.js`` BEFORE ``core/conversations.js`` so the
    concatenated core bundle preserves the correct load order.
    """
    owners = runtime_section_names()
    reducer_idx = owners.index('core/conv_reducers.js')
    conv_idx = owners.index('core/conversations.js')
    assert reducer_idx >= 0, (
        "lib/js_bundler.py::_BUNDLE_FILES must include "
        "'core/conv_reducers.js' — bundler ratchet skipped"
    )
    assert conv_idx >= 0, (
        "lib/js_bundler.py::_BUNDLE_FILES must still include "
        "'core/conversations.js'"
    )
    assert reducer_idx < conv_idx, (
        "'core/conv_reducers.js' must appear BEFORE "
        "'core/conversations.js' in _BUNDLE_FILES — leaf must load "
        "before its downstream reader"
    )


if __name__ == '__main__':
    for fn in [
        test_conv_reducers_leaf_module_exists_and_declares_five,
        test_conversations_js_no_longer_declares_the_five_reducers,
        test_bundle_manifest_loads_reducers_before_conversations,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
