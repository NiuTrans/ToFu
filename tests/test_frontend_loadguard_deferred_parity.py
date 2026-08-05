# Incident anchor: born in commit 4b7e7832 — fix(frontend): drop openDailyReport from LoadGuard stubs — deferred f...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Guards for pt_248c41b0 — LoadGuard must never pre-stub a DEFERRED
entry point (the openDailyReport residue).

The trap (sub-9C toggleMemory precedent): index.html's LoadGuard
pre-installs `window[name] = function() { _notReady(); }` for every name
in its `var stubs = [...]` list. feature-loader's _installFeatureStub
REFUSES to clobber an existing function (`typeof window[name] ===
'function'` → skip), so a LoadGuard-stubbed deferred entry point never
gets the lazy stub — clicking the button before the idle prefetch lands
(~2s) toasts "please wait" and NEVER triggers the bundle load. With the
stub installed instead, the same click would load the feature bundle and
dispatch.

Rule: LoadGuard is for CORE functions only (its window is "core bundle
not yet executed"). Deferred functions (_DEFERRED_ENTRY_POINTS members)
never belong in its list.

The LEGACY_WELCOME four (openOrchestration / openTaskMode /
togglePaperMode / enterImageGenMode) are the deliberate exception,
ratcheted to exactly four: they are the welcome-screen's primary
affordances, where a graceful "please wait" during the CORE-load window
(on slow networks, far longer than the deferred-prefetch window) is
worth more than arming a deferred load that the idle prefetch will
cover a beat later anyway. New deferred entries must NOT join them.

History: openDailyReport was added to LoadGuard in sub-6 before the
rule existed (pt_248c41b0 — the only reverse-direction residue after
sub-9C established the criterion).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'

# The deliberate welcome-screen exception (see module docstring). Ratchet:
# this set must never grow.
LEGACY_WELCOME = frozenset({
    'openOrchestration', 'openTaskMode', 'togglePaperMode', 'enterImageGenMode',
})


def _loadguard_stubs() -> set[str]:
    html = INDEX_HTML.read_text()
    m = re.search(r'var stubs = \[(.*?)\];', html, re.S)
    assert m, 'LoadGuard stub list not found in index.html'
    # Removal comments inside the list ALSO quote names ('toggleMemory'
    # REMOVED …) — strip /* … */ before extracting, or a removed name
    # reads back as present.
    body = re.sub(r'/\*.*?\*/', '', m.group(1), flags=re.S)
    return set(re.findall(r"'([A-Za-z_]+)'", body))


def _deferred_entry_points() -> set[str]:
    import lib.js_bundler as jb
    _bf, _df, ep, _crit = jb._extract_manifest_from_source(str(BUNDLER_PY))
    return set(ep)


def test_open_daily_report_not_in_loadguard():
    """The named residue (pt_248c41b0): openDailyReport is a deferred
    entry point (sub-6) — its LoadGuard stub blocks the lazy stub."""
    assert 'openDailyReport' not in _loadguard_stubs(), (
        'openDailyReport must be removed from the LoadGuard stubs list — '
        'the LoadGuard _notReady stub makes feature-loader skip the lazy '
        'stub, so the topbar My Day button toasts "please wait" and never '
        'loads the bundle when clicked before the prefetch lands')


def test_deferred_entry_points_never_in_loadguard():
    """The rule, both directions: no _DEFERRED_ENTRY_POINTS member may
    appear in the LoadGuard stubs list — except the ratcheted
    LEGACY_WELCOME four."""
    overlap = _deferred_entry_points() & _loadguard_stubs()
    assert overlap <= LEGACY_WELCOME, (
        f'deferred entry points pre-stubbed by LoadGuard (lazy stub would '
        f'never install): {sorted(overlap - LEGACY_WELCOME)}')


def test_legacy_welcome_ratchet():
    """The exception must not grow: exactly these four, no more."""
    overlap = _deferred_entry_points() & _loadguard_stubs()
    assert overlap == LEGACY_WELCOME, (
        f'the LoadGuard∩deferred overlap must stay exactly the '
        f'LEGACY_WELCOME four, got {sorted(overlap)} — removing one is '
        'fine (then shrink this set), adding one is forbidden')


def test_loadguard_still_covers_core_handlers():
    """Control: the LoadGuard list keeps its CORE boot handlers (its
    raison d'être — graceful clicks while the core bundle is loading)."""
    stubs = _loadguard_stubs()
    for name in ('sendMessage', 'newChat', 'handleKeyDown'):
        assert name in stubs, (
            f'{name} must stay LoadGuard-stubbed — it is a core function')
