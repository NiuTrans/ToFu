# Incident anchor: born in commit c9b75f98 — settings decouple pilot: extract translate panel to HTML fragment + s...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Closed-system guard for the settings-panel HTML-fragment injection.

Panels were decoupled out of ``index.html`` into ``static/settings_panels/<tab>.html``
fragments, spliced back at render time by ``lib/settings_panels.inject_panels``
(see the settings decoupling batch, 2026-07-18). This is the SAME class of trap
the JS-bundler allowlist guards (CLAUDE.md §3.2.1): a marker with no fragment,
or a fragment with no marker, or a fragment edit that doesn't invalidate the
served-HTML cache, silently makes a settings tab vanish or go stale.

These tests assert the whole loop is closed so any of those breaks fails HERE
instead of as a mysteriously-missing settings page in the browser.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""
from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANELS_DIR = os.path.join(PROJECT_ROOT, 'static', 'settings_panels')

# Every settings tab id wired in index.html's <nav class="settings-tabs"> via
# switchSettingsTab('<tab>'). This is the ground-truth set the served page MUST
# contain a `settingsTab_<tab>` panel div for — whether inline or spliced from a
# fragment. Derived from the nav buttons, asserted below against the HTML so a
# new tab can't be added without appearing here.
_EXPECTED_TABS = frozenset({
    'general', 'api', 'preset', 'search', 'translate', 'speech', 'network',
    'devices', 'feishu', 'oauth', 'mcp', 'skills', 'preferences', 'advanced',
})


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _index_html():
    return _read(os.path.join(PROJECT_ROOT, 'index.html'))


def _assembled_html():
    """index.html with panel fragments spliced in — what the browser receives."""
    from lib.settings_panels import inject_panels
    return inject_panels(_index_html())


# ── The nav's tab set matches our expected set (catches a new/removed tab) ──

def test_nav_tabs_match_expected_set():
    html = _index_html()
    nav_tabs = set(re.findall(r"switchSettingsTab\('([a-z_]+)'\)", html))
    # switchSettingsTab is also called from a few JS files with 'providers' /
    # 'preferences' etc.; here we only scan index.html's onclick handlers.
    assert nav_tabs == _EXPECTED_TABS, (
        'Settings nav tab set drifted from the expected set. '
        f'nav={sorted(nav_tabs)} expected={sorted(_EXPECTED_TABS)}. '
        'If you added/removed a settings tab, update _EXPECTED_TABS.'
    )


# ── THE core guard: every tab's panel exists in the ASSEMBLED HTML ──────────

def test_all_panels_present_in_assembled_html():
    """After splicing fragments, the served HTML must contain a
    ``id="settingsTab_<tab>"`` panel for EVERY expected tab. A migrated panel
    whose fragment went missing (or whose marker was dropped) fails here."""
    html = _assembled_html()
    missing = [
        tab for tab in _EXPECTED_TABS
        if f'id="settingsTab_{tab}"' not in html
    ]
    assert not missing, (
        'Settings panels ABSENT from the assembled (served) HTML — a fragment '
        f'or its marker is missing: {sorted(missing)}'
    )


def test_no_unresolved_markers_in_assembled_html():
    """After injection there must be NO ``<!-- SETTINGS_PANEL:x -->`` marker
    left — an unresolved marker means the fragment file was missing (the
    injector leaves it in place and logs an error rather than vanish it)."""
    from lib.settings_panels import find_markers
    leftover = find_markers(_assembled_html())
    assert not leftover, (
        'Unresolved SETTINGS_PANEL markers after injection (fragment files '
        f'missing on disk): {sorted(set(leftover))}'
    )


# ── Marker ↔ fragment bidirectional parity ──────────────────────────────────

def test_every_marker_has_a_fragment_file():
    from lib.settings_panels import find_markers, fragment_path
    orphan_markers = [
        tab for tab in set(find_markers(_index_html()))
        if not os.path.exists(fragment_path(tab))
    ]
    assert not orphan_markers, (
        'index.html has SETTINGS_PANEL markers with NO fragment file on disk '
        f'(marker → silent empty panel): {sorted(orphan_markers)}'
    )


def test_every_fragment_file_has_a_marker():
    from lib.settings_panels import find_markers, list_fragment_tabs
    markers = set(find_markers(_index_html()))
    orphan_fragments = sorted(list_fragment_tabs() - markers)
    assert not orphan_fragments, (
        'Fragment files under static/settings_panels/ with NO marker in '
        f'index.html (dead fragment, never served): {orphan_fragments}'
    )


def test_fragment_defines_its_own_panel():
    """A fragment for tab X must actually contain the ``settingsTab_X`` panel
    div — guards a copy-paste that ships the wrong tab's markup."""
    from lib.settings_panels import list_fragment_tabs, fragment_path
    for tab in list_fragment_tabs():
        frag = _read(fragment_path(tab))
        assert f'id="settingsTab_{tab}"' in frag, (
            f'Fragment {tab}.html does not define id="settingsTab_{tab}" '
            '(wrong tab markup?).'
        )


# ── Cache-key safety: fragment edits must invalidate the served-HTML cache ──

def test_panels_signature_changes_when_a_fragment_changes(tmp_path, monkeypatch):
    """A fragment edit MUST change panels_signature() — else index_page's HTML
    cache would serve stale markup after a panel edit (the silent-no-op trap
    this whole mechanism must avoid)."""
    import lib.settings_panels as sp

    d = tmp_path / 'settings_panels'
    d.mkdir()
    (d / 'translate.html').write_text('<div id="settingsTab_translate"></div>')
    monkeypatch.setattr(sp, 'PANELS_DIR', str(d))

    sig1 = sp.panels_signature()
    assert sig1, 'signature should be non-empty when a fragment exists'

    # Rewrite with different content + bump mtime so (mtime,size) both move.
    frag = d / 'translate.html'
    frag.write_text('<div id="settingsTab_translate"><p>changed and longer</p></div>')
    os.utime(str(frag), (10**9, 10**9))  # deterministic distinct mtime
    sig2 = sp.panels_signature()
    assert sig2 != sig1, (
        'panels_signature() did NOT change after a fragment edit — index_page '
        'would serve stale HTML.'
    )


def test_index_cache_key_includes_panels_signature():
    """Structural proof that routes.common threads panels_signature() into the
    served-HTML cache decision (not just computes it and forgets it)."""
    import inspect
    from routes import common
    src = inspect.getsource(common.index_page)
    assert '_settings_panels_signature()' in src, (
        'index_page no longer calls _settings_panels_signature() — fragment '
        'edits would not invalidate the HTML cache.'
    )
    assert "_bundled_index_cache['panels']" in src, (
        'index_page cache-hit check no longer compares the panels signature — '
        'a fragment edit would serve stale cached HTML.'
    )


# ── ALL panels decoupled: the settings modal region is markers-only ────────

def test_all_panels_decoupled_not_inline():
    """The whole-batch invariant: index.html must NOT inline ANY settings panel
    body — every tab is a `<!-- SETTINGS_PANEL:x -->` marker + a fragment file.
    A future edit that re-inlines a panel (or forgets its marker) fails here.

    Note: sibling MODALS (mcpAddOverlay, skillsFilesOverlay, …) legitimately
    remain inline — they are not `settingsTab_*` panels, so this only asserts
    the panel DIVs themselves moved out."""
    html = _index_html()
    still_inline = re.findall(r'id="settingsTab_([a-z_]+)"', html)
    assert not still_inline, (
        'index.html still inlines these settings panels (should be markers + '
        f'fragments): {sorted(still_inline)}'
    )
    # Every expected tab has its marker present.
    missing_markers = [
        tab for tab in _EXPECTED_TABS
        if f'<!-- SETTINGS_PANEL:{tab} -->' not in html
    ]
    assert not missing_markers, (
        f'index.html missing SETTINGS_PANEL markers for: {sorted(missing_markers)}'
    )


def test_translate_fragment_keeps_its_mt_markup():
    """Spot-check one fragment's content survived extraction intact (the pilot,
    with the most distinctive markup)."""
    frag = _read(os.path.join(PANELS_DIR, 'translate.html'))
    assert 'id="mtCardNiutrans"' in frag and 'id="settingMtEnabled"' in frag, (
        'translate.html fragment is missing its expected MT provider markup.'
    )


# ── CSS relocation drift guard (settings.css vs styles.css) ─────────────────
#
# Page-specific settings CSS was moved out of styles.css into settings.css so a
# settings page's styles live near its markup+logic. The half-migration trap:
# a prefix ends up SPLIT across both files — some rules moved, some left — so a
# future editor can't tell which file owns the page and cascade surprises creep
# in. These prefixes are page-specific selectors that MUST live ONLY in
# settings.css (0 top-level occurrences in styles.css). Shared chrome
# (.settings-*, .stg-btn-*, .settings-toggle, …) intentionally stays in
# styles.css and is NOT listed here.
#
# NOTE: this list grows per CSS-migration commit (A→B→C). An entry is added
# ONLY once its rules are actually in settings.css, so the guard stays green at
# every commit boundary while still locking each migrated page against drift.
_SETTINGS_ONLY_CSS_PREFIXES = (
    r'\.mt-provider', r'\.mt-apply',        # translate (pilot)
    r'\.oauth-',                            # oauth      (batch A)
    r'\.pref-', r'\.prefs\b',               # preferences(batch A)
    r'\.feishu-',                           # feishu     (batch A)
    r'\.mcp-',                              # mcp        (batch B)
    r'\.skills-',                           # skills     (batch B)
    r'\.stg-matrix', r'\.stg-mx-',          # api / access-matrix widget (batch C)
    r'\.sysprompt',                         # general / system-prompt editor (batch C)
    r'\.stg-dropdown-visibility',           # preset / display (batch C)
    r'\.chip\b', r'\.auth-src',             # search / network (batch D)
    r'\.two-col',                           # settings row layout (batch D)
    # devices — the agents table / token rows / load-failure state, plus the
    # SAME feature's remote-device picker in the project folder browser. Added
    # when the page got styles AT ALL: every one of these classes was used by
    # the shipped markup with zero rules behind it, so the panel rendered as
    # browser defaults (see tests/test_devices_panel_and_platform_download.py).
    # The shared chrome it sits on (.stg-table / .stg-row / .stg-desc /
    # .stg-dim / bare .stg-btn) stays in styles.css and is NOT listed here.
    r'\.devices-', r'\.remote-agent', r'\.remote-root', r'\.remote-devices',
)

# Documented cascade-safety exceptions: rules that MUST stay in styles.css even
# though their prefix migrated — because they are nested inside a shared @media
# block (moving them would orphan the media context) OR are genuinely shared
# with a non-settings feature. Keep this list tiny and justified; each entry is
# a substring that, if the ONLY styles.css occurrence(s) of a prefix, is allowed.
_CSS_STYLES_ALLOWED_LEFTOVERS = frozenset()


def _styles_css():
    return _read(os.path.join(PROJECT_ROOT, 'static', 'styles.css'))


def _settings_css():
    return _read(os.path.join(PROJECT_ROOT, 'static', 'settings.css'))


def _strip_css_comments(css):
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def test_settings_css_prefixes_not_left_in_styles():
    """Half-migration guard: every page-specific prefix that was moved to
    settings.css must have ZERO occurrences left in styles.css. A prefix
    appearing in BOTH files is the split-brain state this guards against
    (owner-mandated, same weight as the panel guard)."""
    styles = _strip_css_comments(_styles_css())
    leftovers = {}
    for pat in _SETTINGS_ONLY_CSS_PREFIXES:
        hits = re.findall(pat, styles)
        if hits:
            leftovers[pat] = len(hits)
    assert not leftovers, (
        'Page-specific settings CSS prefixes STILL present in styles.css '
        '(half-migration / split-brain — they belong ONLY in settings.css): '
        f'{leftovers}'
    )


def test_settings_css_prefixes_present_in_settings():
    """Reverse: each migrated prefix must actually appear in settings.css (so
    the styles didn't just get deleted). Guards an over-eager removal."""
    settings = _strip_css_comments(_settings_css())
    missing = [pat for pat in _SETTINGS_ONLY_CSS_PREFIXES
               if not re.search(pat, settings)]
    assert not missing, (
        'Migrated prefixes absent from settings.css (styles deleted, not '
        f'moved?): {missing}'
    )


def test_settings_css_link_after_styles_in_index():
    """Cascade order: the settings.css <link> MUST come AFTER styles.css so
    equal-specificity overrides resolve the same as before the move."""
    html = _index_html()
    i_styles = html.find('href="static/styles.css"')
    i_settings = html.find('href="static/settings.css"')
    assert i_styles != -1 and i_settings != -1, 'both stylesheet links must exist'
    assert i_styles < i_settings, (
        'settings.css <link> must come AFTER styles.css (cascade order) — '
        'moved rules would otherwise change layering vs before the extraction.'
    )
