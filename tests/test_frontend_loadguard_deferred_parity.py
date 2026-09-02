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
not yet executed"). Deferred functions (_FEATURE_ENTRY_POINTS members)
never belong in its list.

There is no exception: the Vite bridge queues a first click while its module
entry initializes, so welcome-screen affordances also belong exclusively to
feature-loader.

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
MAIN_TS = ROOT / 'frontend' / 'src' / 'main.ts'

LEGACY_WELCOME = frozenset()


def _loadguard_stubs() -> set[str]:
    html = INDEX_HTML.read_text()
    m = re.search(r'var stubs = \[(.*?)\];', html, re.S)
    if not m:
        assert 'LoadGuard' not in html, (
            'a partial classic LoadGuard remains in index.html without its '
            'auditable stub list')
        return set()
    # Removal comments inside the list ALSO quote names ('toggleMemory'
    # REMOVED …) — strip /* … */ before extracting, or a removed name
    # reads back as present.
    body = re.sub(r'/\*.*?\*/', '', m.group(1), flags=re.S)
    return set(re.findall(r"'([A-Za-z_]+)'", body))


def _deferred_entry_points() -> set[str]:
    source = MAIN_TS.read_text()
    bodies = re.findall(r'\w+Entries\s*=\s*new Set\(\[(.*?)\]\)', source, re.S)
    assert bodies, 'Vite feature-domain entry sets not found in frontend/src/main.ts'
    return set(re.findall(r"['\"]([A-Za-z_$][\w$]*)['\"]", '\n'.join(bodies)))


def test_open_daily_report_not_in_loadguard():
    """The named residue (pt_248c41b0): openDailyReport is a deferred
    entry point (sub-6) — its LoadGuard stub blocks the lazy stub."""
    assert 'openDailyReport' not in _loadguard_stubs(), (
        'openDailyReport must be removed from the LoadGuard stubs list — '
        'the LoadGuard _notReady stub makes feature-loader skip the lazy '
        'stub, so the topbar My Day button toasts "please wait" and never '
        'loads the bundle when clicked before the prefetch lands')


def test_deferred_entry_points_never_in_loadguard():
    """The rule, both directions: no _FEATURE_ENTRY_POINTS member may
    appear in the LoadGuard stubs list."""
    overlap = _deferred_entry_points() & _loadguard_stubs()
    assert overlap <= LEGACY_WELCOME, (
        f'deferred entry points pre-stubbed by LoadGuard (lazy stub would '
        f'never install): {sorted(overlap - LEGACY_WELCOME)}')


def test_legacy_welcome_ratchet():
    """The Vite bridge removed the last LoadGuard exception."""
    overlap = _deferred_entry_points() & _loadguard_stubs()
    assert overlap == LEGACY_WELCOME, (
        f'the LoadGuard∩deferred overlap must stay empty, got {sorted(overlap)}')


def test_loadguard_still_covers_core_handlers():
    """Core handlers use the Vite action registry after LoadGuard removal."""
    assert not _loadguard_stubs()
    html = INDEX_HTML.read_text()
    main = MAIN_TS.read_text()
    for name in ('sendMessage', 'newChat', 'handleKeyDown'):
        assert name in html, f'{name} has no declarative HTML action surface'
    assert 'installActionRegistry' in main
