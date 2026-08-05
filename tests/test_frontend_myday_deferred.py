"""Guards for pt_3879f00e sub-part 6 — defer myday.js (57KB) +
myday_tasks.js (8KB) from the CORE boot bundle into _DEFERRED_FILES.

My Day (daily activity report modal) opens only via the topbar button —
never on first paint.

Census (2026-08-01, all grep-verified):
  * ZERO external JS callers: openDailyReport / closeDailyReport /
    _mydayTriggerGenerate are referenced ONLY from index.html inline
    onclicks (comm intersection of every index.html onclick target with
    every function defined in the two modules = exactly these three),
  * `_myday` state is referenced by myday.js + myday_tasks.js ONLY
    (grep -l '_myday\\b' across static/js, bundles excluded) — the two
    modules move together, order preserved inside _DEFERRED_FILES,
  * load-time side effects self-bootstrap on late arrival: both boot
    blocks (`_mydayScheduleReminder`, `_mydayBootDayDigestSoon`) branch
    on document.readyState === 'loading' and call DIRECTLY when the
    document is already parsed — which is always the case when the
    feature bundle lands after boot. The digest boot is already
    setTimeout(2500) by design ('never competes with first paint') —
    deferral aligns with the module's own intent,
  * no push / SSE / cross-tab coupling (no pushSubscribe, no
    _VU_FORWARD, no message listener in the modules).

THREE feature-loader stubs (py + js tables): the topbar My Day button
is static always-visible HTML, so openDailyReport is a genuine
early-click entry point; closeDailyReport + _mydayTriggerGenerate are
only reachable inside the open modal but are stubbed for
defense-in-depth (image-gen precedent).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
MYDAY = ROOT / 'static' / 'js' / 'myday.js'
MYDAY_TASKS = ROOT / 'static' / 'js' / 'myday_tasks.js'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'
ENTRY = 'myday.js'
ENTRY_TASKS = 'myday_tasks.js'
STUBS = ('openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate')


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move (failing-first drivers)
# ---------------------------------------------------------------------------
def test_myday_in_deferred_files():
    _bf, deferred, _ep, _crit = _manifest()
    assert ENTRY in deferred, (
        f"'{ENTRY}' must be in _DEFERRED_FILES — 57KB of report-modal "
        'rendering out of the render-blocking core')


def test_myday_not_in_core_bundle_files():
    bundle, _df, _ep, _crit = _manifest()
    assert ENTRY not in bundle, (
        f"'{ENTRY}' must NOT remain in _BUNDLE_FILES — listing it in both "
        'bundles would duplicate the _myday state object')


def test_myday_tasks_moves_with_myday():
    bundle, deferred, _ep, _crit = _manifest()
    assert ENTRY_TASKS in deferred, (
        f"'{ENTRY_TASKS}' must move WITH myday.js — it shares the _myday "
        'state object and is invoked from myday-rendered onclicks')
    assert ENTRY_TASKS not in bundle, (
        f"'{ENTRY_TASKS}' must NOT remain in _BUNDLE_FILES")
    assert deferred.index(ENTRY) < deferred.index(ENTRY_TASKS), (
        'myday.js must precede myday_tasks.js inside _DEFERRED_FILES — '
        'the tasks module reads the _myday state myday.js declares')


# ---------------------------------------------------------------------------
# 2. entry-point stubs, py + js dual tables (failing-first drivers)
# ---------------------------------------------------------------------------
def test_entry_points_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    for name in STUBS:
        assert name in entry_points, (
            f'{name} must be a _DEFERRED_ENTRY_POINTS member — the topbar '
            'My Day button is always-visible static HTML, so the opener '
            'is a genuine early-click entry point')


def test_entry_points_in_js_table():
    loader = FEATURE_LOADER.read_text()
    for name in STUBS:
        assert f"'{name}'" in loader, (
            f'{name} must be in feature-loader.js _DEFERRED_ENTRY_POINTS '
            '(py/js tables must agree)')


# ---------------------------------------------------------------------------
# 3. module self-containment (controls)
# ---------------------------------------------------------------------------
def test_myday_state_private_to_the_two_modules():
    import subprocess
    out = subprocess.run(
        ['grep', '-rl', '_myday\\b', 'static/js', '--include=*.js'],
        cwd=ROOT, capture_output=True, text=True).stdout
    users = {l.strip() for l in out.splitlines()
             if l.strip() and 'bundle-' not in l and 'feature-' not in l}
    assert users == {'static/js/myday.js', 'static/js/myday_tasks.js'}, (
        f'_myday state must stay private to the two moving modules, got {users}')


def test_boot_side_effects_are_late_load_safe():
    src = MYDAY.read_text()
    assert re.search(
        r"document\.readyState === 'loading'\)\s*\{\s*document\.addEventListener\('DOMContentLoaded', _mydayScheduleReminder\)",
        src), ('_mydayScheduleReminder boot block must keep its readyState '
               'branch so it fires directly when the feature bundle lands '
               'after DOMContentLoaded')
    assert re.search(
        r"document\.readyState === 'loading'\)\s*\{\s*document\.addEventListener\('DOMContentLoaded', _mydayBootDayDigestSoon\)",
        src), ('_mydayBootDayDigestSoon boot block must keep its readyState '
               'branch for the same late-arrival reason')


def test_dev_fallback_script_tags_kept():
    html = INDEX_HTML.read_text()
    assert 'static/js/myday.js' in html, (
        'index.html must carry the myday.js dev-fallback <script> tag')
    assert 'static/js/myday_tasks.js' in html, (
        'index.html must carry the myday_tasks.js dev-fallback <script> tag')


def test_loadguard_stub_list_covers_opener():
    """The pre-boot LoadGuard stub list (index.html inline) must include
    openDailyReport — the topbar My Day button is always-visible static
    HTML, clickable in the sub-second window before the core bundle
    executes and feature-loader installs its stub; the LoadGuard stub
    turns that click into 'please wait' instead of a ReferenceError."""
    html = INDEX_HTML.read_text()
    m = re.search(r'var stubs = \[(.*?)\];', html, re.S)
    assert m and "'openDailyReport'" in m.group(1), (
        "index.html's LoadGuard stub list must include 'openDailyReport'")
