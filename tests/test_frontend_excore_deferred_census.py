#!/usr/bin/env python3
"""Derived census: NO core-bundle file may call a symbol that lives in an
ex-core DEFERRED module without a typeof guard.

WHY THIS EXISTS (measured incident 2026-08-01 — "sidebar folder rail gone")
--------------------------------------------------------------------------
Epic-E sub-3B deferred ``core/health_stream_timer.js`` out of the boot bundle.
Its pre-flight census audited a HAND-PICKED symbol list
(``twStart|twUpdate|twStop|_setStreamDegraded``) and missed
``_checkDbHealth`` — which ``main.js``'s synchronous boot IIFE called
UNGUARDED. The served page then crashed at boot with
``ReferenceError: _checkDbHealth is not defined`` *before* ``initActiveTasks``,
so neither ``loadConversationsFromServer`` nor ``loadFolders`` ever ran:
the sidebar rendered without conversations (until cross-tab sync painted
them in from a surviving tab) and without the folder rail — reproduced
headlessly against the live server (zero ``GET /api/v1/folders`` in the
access log all day, zero children under ``#folderTabs``).

The lesson is structural: a census over a hand-picked symbol list rots the
moment the module grows a new exported function. The only list that cannot
drift is the one DERIVED from the module's own top-level definitions — same
argument as ``conv_family_sources`` vs symbol pins (tests/_conv_bundle_sources.py).

WHAT THIS GUARDS
----------------
For every EX-CORE deferred module (a file that used to be in the core bundle
and still has core-bundle consumers — today: ``core/cross_tab_sync.js`` and
``core/health_stream_timer.js``; a future deferral of the same kind JOINS
this list):

  1. Parse its top-level ``function`` / ``async function`` definitions.
  2. Scan every CORE-bundle file (``_BUNDLE_FILES``) for calls to those
     names. A call is LEGAL only when a
     ``typeof (window.)?<name> === 'function'`` guard appears on the same
     line or within the preceding 3 lines (the established pattern).
  3. Fail with the full file:line list when any unguarded call remains.

Plus the positive pin: the boot/recovery primitives ``_checkDbHealth`` and
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

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

sys.path.insert(0, HERE)
from _conv_bundle_sources import bundle_files, files_defining  # noqa: E402


# Modules that USED to be core and were deferred with core consumers left
# behind. A future deferral of the same kind MUST be added here (that act is
# exactly the moment the census is needed — it fails until every core call
# site is typeof-guarded).
_EX_CORE_DEFERRED = ('core/cross_tab_sync.js', 'core/health_stream_timer.js')

_DEF_RE = re.compile(r'^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(', re.M)


def _core_files():
    from lib.js_bundler import _BUNDLE_FILES
    return list(_BUNDLE_FILES)


def _deferred_symbols():
    syms = set()
    for rel in _EX_CORE_DEFERRED:
        src = open(os.path.join(JS_DIR, rel), encoding='utf-8').read()
        syms.update(_DEF_RE.findall(src))
    return sorted(syms)


def test_excore_deferred_symbols_have_no_unguarded_core_calls():
    shipped_core = _core_files()
    symbols = _deferred_symbols()
    assert symbols, 'derived symbol list is empty — the derivation broke'
    violations = []
    for rel in shipped_core:
        path = os.path.join(JS_DIR, rel)
        try:
            lines = open(path, encoding='utf-8').read().splitlines()
        except OSError:
            continue
        in_block_comment = False
        for i, line in enumerate(lines, 1):
            # Block-comment state machine (/* … */ may span lines whose
            # interior lines carry no `*` leader — conv_sync_push.js's
            # indented prose style). Line comments skipped too. This is the
            # heuristic layer, so it is deliberately simple: it does not
            # parse strings, but no core file opens a block comment inside
            # a string on a code line we scan.
            code_line = line
            if in_block_comment:
                if '*/' in code_line:
                    in_block_comment = False
                    code_line = code_line.split('*/', 1)[1]
                else:
                    continue
            while '/*' in code_line:
                head, _sep, tail = code_line.partition('/*')
                if '*/' in tail:
                    code_line = head + tail.split('*/', 1)[1]
                else:
                    code_line = head
                    in_block_comment = True
                    break
            stripped = code_line.lstrip()
            if not stripped or stripped.startswith('//'):
                continue
            # Inline onclick= handlers resolve at CLICK time via window scope
            # and are the accepted user-triggered entry pattern into deferred
            # modules (same class as feature-loader stubs): a dead button on
            # feature-bundle failure is the designed degradation, not a boot
            # crash. Only automatic (script-driven) calls are gated here.
            if 'onclick=' in code_line:
                continue
            for name in symbols:
                if not re.search(r'\b' + re.escape(name) + r'\s*\(', code_line):
                    continue
                ctx = '\n'.join(lines[max(0, i - 6):i])
                pos_guard = (r"typeof\s+(?:window\.)?" + re.escape(name)
                             + r"\s*===\s*['\"]function['\"]")
                if re.search(pos_guard, code_line) or re.search(pos_guard, ctx):
                    continue
                # The codebase's second guard idiom: an early-return gate at
                # the top of the driving function (conv_verify_retry.js:48 —
                # `if (typeof X !== 'function') return;`). The setTimeout
                # callback it schedules closes over the guarded activation,
                # so the call 13 lines below is genuinely covered. The
                # 15-line lookback is brace-blind by design — a ratchet for
                # review, not a proof.
                neg_ctx = '\n'.join(lines[max(0, i - 16):i])
                neg_guard = (r"typeof\s+(?:window\.)?" + re.escape(name)
                             + r"\s*!==\s*['\"]function['\"]")
                if re.search(neg_guard, neg_ctx):
                    continue
                violations.append(f'{rel}:{i} {name}')
    assert not violations, (
        'unguarded core→deferred call sites (each ReferenceErrors the moment '
        'it runs before the feature bundle lands — the main.js:1199 '
        '_checkDbHealth boot crash was one of these):\n  '
        + '\n  '.join(violations))


def test_boot_recovery_primitives_live_in_core():
    """``_checkDbHealth`` (boot IIFE) and ``_checkServerHealth`` (poll-fallback
    circuit breaker) are boot/recovery primitives — they must be defined by a
    CORE-bundle file. A re-deferral flips this RED at review time instead of
    crashing user boots in production."""
    core = set(_core_files())
    for sym in ('_checkDbHealth', '_checkServerHealth'):
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
    from lib.js_bundler import _BUNDLE_FILES, _DEFERRED_FILES
    for rel in _EX_CORE_DEFERRED:
        assert rel in _DEFERRED_FILES and rel not in _BUNDLE_FILES, (
            f'{rel} left _DEFERRED_FILES — re-audit whether this census '
            'still covers the right module set')


if __name__ == '__main__':
    test_excore_deferred_modules_stay_deferred()
    test_boot_recovery_primitives_live_in_core()
    test_excore_deferred_symbols_have_no_unguarded_core_calls()
    print('ALL PASSED')
