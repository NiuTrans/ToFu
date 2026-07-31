#!/usr/bin/env python3
"""Wire-parity for pt_3879f00e sub-part 2 slice 3 — persist-helpers cluster.

Scope: move the 6-function pure-helper cluster out of core/conversations.js
(2207 L post-slice-2) into a new leaf module core/conv_persist_helpers.js:

  * _stripUsageTransient(u)                          — strip _wire_fp/_wire_static
  * _trimMsgForPersist(m)                            — segments/toolRounds/apiRounds cleanse for PUT
  * _serverHasSegmentsLocalLacks(serverMsgs, local)  — segments-freshness signal
  * _serverHasTranslationLocalLacks(serverMsgs, local) — translation-freshness signal
  * _isErrorOnlyAssistant(m)                         — error-only-tail probe for rebase drop
  * _rebaseUnackedTail(serverMsgs, localMsgs)        — 409 CAS rebase

Plus the module-local constant:
  * const _USAGE_TRANSIENT_KEYS = ['_wire_fp', '_wire_static']

All 6 helpers are PURE reducers over their arguments — zero runtime state,
zero IIFE-load side effects. Consumers are exclusively inside conversations.js
(bare-name reads at call time inside function bodies: _trimMsgForPersist at
L517, _stripUsageTransient at L202/L209, _rebaseUnackedTail at L643,
_serverHasSegmentsLocalLacks at L1919, _serverHasTranslationLocalLacks at
L1929, _isErrorOnlyAssistant at L342/L366). Bundle-ordering (leaf BEFORE
conversations.js) is sufficient.

Failing-first — 3 assertions (RED before extraction, GREEN after):

  1. static/js/core/conv_persist_helpers.js exists AND declares all 6
     helpers via `function <name>(` AND exposes each on window.*.
  2. static/js/core/conversations.js NO LONGER declares any of the 6.
  3. lib/js_bundler.py::_BUNDLE_FILES lists 'core/conv_persist_helpers.js'
     BEFORE 'core/conversations.js'.
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


_HELPERS = (
    '_stripUsageTransient',
    '_trimMsgForPersist',
    '_serverHasSegmentsLocalLacks',
    '_serverHasTranslationLocalLacks',
    '_isErrorOnlyAssistant',
    '_rebaseUnackedTail',
)


def _read(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), encoding='utf-8') as f:
        return f.read()


@_unit
def test_conv_persist_helpers_leaf_module_exists_and_declares_six():
    """Slice 3: static/js/core/conv_persist_helpers.js exists and defines
    all 6 helpers via `function <name>(` AND exposes each on window.*."""
    src = _read('static/js/core/conv_persist_helpers.js')
    for name in _HELPERS:
        assert re.search(rf'\bfunction\s+{re.escape(name)}\s*\(', src), (
            f'core/conv_persist_helpers.js must define `function {name}(...)` '
            f'— extraction incomplete'
        )
        assert f'window.{name} = {name}' in src, (
            f'core/conv_persist_helpers.js must expose window.{name} = {name} '
            f'so downstream typeof-guarded reads resolve'
        )
    # The module-level constant is load-bearing (_stripUsageTransient reads it).
    assert "_USAGE_TRANSIENT_KEYS = ['_wire_fp', '_wire_static']" in src, (
        'core/conv_persist_helpers.js must include the '
        '_USAGE_TRANSIENT_KEYS module-level constant'
    )


@_unit
def test_conversations_js_no_longer_declares_persist_helpers():
    """Slice 3: conversations.js MUST NOT still carry a `function <name>(`
    definition for any of the 6 persist helpers."""
    src = _read('static/js/core/conversations.js')
    for name in _HELPERS:
        m = re.search(rf'\bfunction\s+{re.escape(name)}\s*\(', src)
        assert not m, (
            f'conversations.js must NOT still declare `function {name}(...)` '
            f'— leftover definition would shadow the extracted leaf'
        )
    # The constant must also be gone from conversations.js (or the extract
    # is only half-done and the two files carry duplicate declarations that
    # will conflict on the second-parse `const` re-declaration).
    m = re.search(
        r"^const\s+_USAGE_TRANSIENT_KEYS\s*=", src, re.M
    )
    assert not m, (
        'conversations.js must NOT still declare `const _USAGE_TRANSIENT_KEYS '
        '= ...` — extraction incomplete (duplicate `const` in the bundle '
        'would SyntaxError)'
    )


@_unit
def test_bundle_manifest_loads_persist_helpers_before_conversations():
    """Slice 3: lib/js_bundler.py must list 'core/conv_persist_helpers.js'
    BEFORE 'core/conversations.js'."""
    src = _read('lib/js_bundler.py')
    persist_idx = src.find("'core/conv_persist_helpers.js'")
    conv_idx = src.find("'core/conversations.js'")
    assert persist_idx > 0, (
        "lib/js_bundler.py::_BUNDLE_FILES must include "
        "'core/conv_persist_helpers.js' — bundler ratchet skipped"
    )
    assert conv_idx > 0, (
        "lib/js_bundler.py::_BUNDLE_FILES must still include "
        "'core/conversations.js'"
    )
    assert persist_idx < conv_idx, (
        "'core/conv_persist_helpers.js' must appear BEFORE "
        "'core/conversations.js' in _BUNDLE_FILES — leaf must load "
        "before its downstream reader"
    )


if __name__ == '__main__':
    for fn in [
        test_conv_persist_helpers_leaf_module_exists_and_declares_six,
        test_conversations_js_no_longer_declares_persist_helpers,
        test_bundle_manifest_loads_persist_helpers_before_conversations,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
