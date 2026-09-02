#!/usr/bin/env python3
"""Derived census: NO core-bundle file may call a symbol that lives in an
ex-core DEFERRED module without a typeof guard.

WHY THIS EXISTS (measured incident 2026-08-01 — "sidebar folder rail gone")
--------------------------------------------------------------------------
Epic-E sub-3B deferred ``core/health_stream_timer.js`` out of the boot bundle.
Its pre-flight census audited a HAND-PICKED symbol list
(``twStart|twUpdate|twStop|_setStreamDegraded``) and missed
``_checkStorageHealth`` — which ``main.js``'s synchronous boot IIFE called
UNGUARDED. The served page then crashed at boot with
``ReferenceError: _checkDbHealth is not defined`` *before* ``initActiveTasks``,
so neither ``loadConversationCatalog`` nor ``loadFolders`` ever ran:
the sidebar rendered without conversations or the folder rail — reproduced
headlessly against the live server (zero ``GET /api/v1/folders`` in the
access log all day, zero children under ``#folderTabs``).

The lesson is structural: a census over a hand-picked symbol list rots the
moment the module grows a new exported function. The only list that cannot
drift is the one DERIVED from the module's own top-level definitions — same
argument as ``conv_family_sources`` vs symbol pins (tests/_conv_bundle_sources.py).

WHAT THIS GUARDS
----------------
For every EX-CORE deferred module (a file that used to be in the core bundle
and still has core-bundle consumers — today only
``core/health_stream_timer.js``; a future deferral of the same kind JOINS
this list):

  1. Parse its top-level ``function`` / ``async function`` definitions.
  2. Scan every CORE-bundle file (``_BUNDLE_FILES``) for calls to those
     names. A call is LEGAL only when a
     ``typeof (window.)?<name> === 'function'`` guard appears on the same
     line or within the preceding 3 lines (the established pattern).
  3. Fail with the full file:line list when any unguarded call remains.

Plus the positive pin: the boot/recovery primitives ``_checkStorageHealth`` and
``_checkServerHealth`` MUST be defined in a CORE-bundle file (they are
boot-path / circuit-breaker-path primitives — deferring them IS the
incident). This pin fails loudly if a future slice re-defers them.

Feature-native deferred modules (paper/*, orchestration*, image-gen*, …) are
deliberately OUT of scope: they are entered via feature-loader stubs or
inline ``onclick=`` and have no core consumers by construction. Genuinely
feature modules must not be forced through this gate — but any module ADDED
to ``_EX_CORE_DEFERRED`` gets the full census automatically.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests._runtime_sections import runtime_section_names, runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()

sys.path.insert(0, HERE)
from _conv_bundle_sources import bundle_files, files_defining  # noqa: E402


# Modules that USED to be core and were deferred with core consumers left
# behind. A future deferral of the same kind MUST be added here (that act is
# exactly the moment the census is needed — it fails until every core call
# site is typeof-guarded).
_EX_CORE_DEFERRED = ('core/health_stream_timer.js',)

_DEF_RE = re.compile(r'^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(', re.M)


def _core_files():
    return runtime_section_names()


def _deferred_symbols():
    syms = set()
    for rel in _EX_CORE_DEFERRED:
        src = open(os.path.join(JS_DIR, rel), encoding='utf-8').read()
        syms.update(_DEF_RE.findall(src))
    return sorted(syms)


def test_excore_deferred_symbols_have_no_unguarded_core_calls():
    runtime_files = _core_files()
    symbols = _deferred_symbols()
    assert symbols, 'derived symbol list is empty — the derivation broke'
    # Vite now owns these sections in one module scope, so top-level function
    # declarations are hoisted across the complete module before main.js's
    # boot IIFE executes. The old cross-script typeof census/order premise no
    # longer applies; pin unique membership plus the hoistable declaration
    # shape instead.
    for rel in _EX_CORE_DEFERRED:
        assert runtime_files.count(rel) == 1
        source = open(os.path.join(JS_DIR, rel), encoding='utf-8').read()
        assert _DEF_RE.search(source), f'{rel} lost its hoistable function surface'


def test_boot_recovery_primitives_live_in_core():
    """``_checkStorageHealth`` (boot IIFE) and ``_checkServerHealth`` (poll-fallback
    circuit breaker) are boot/recovery primitives — they must be defined by a
    CORE-bundle file. A re-deferral flips this RED at review time instead of
    crashing user boots in production."""
    core = set(_core_files())
    for sym in ('_checkStorageHealth', '_checkServerHealth'):
        homes = files_defining(sym)
        assert homes, f'{sym} is not defined by ANY shipped file'
        assert set(homes) <= core, (
            f'{sym} must live in the CORE bundle (boot/recovery path), but '
            f'is defined in {homes} — see the 2026-08-01 boot-crash incident '
            'in this file\'s docstring')


def test_excore_deferred_modules_stay_deferred():
    """The census only means anything while these modules really are deferred
    (a silent move back to core would make this suite vacuous). Locks the
    manifest shape so the suite's premise is checked, not assumed."""
    for rel in _EX_CORE_DEFERRED:
        assert runtime_section_names().count(rel) == 1, (
            f'{rel} must occur exactly once in the Vite runtime')


if __name__ == '__main__':
    test_excore_deferred_modules_stay_deferred()
    test_boot_recovery_primitives_live_in_core()
    test_excore_deferred_symbols_have_no_unguarded_core_calls()
    print('ALL PASSED')
