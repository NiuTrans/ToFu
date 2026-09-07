"""tests/test_maintainer_only_settings.py — maintainer-only settings tabs.

Pins the owner-dogfooding contract: most iteration feedback on this project
comes from the maintainer, so the settings UI stays deliberately detailed in
personal/internal builds, while tabs in
``lib/settings_panels.MAINTAINER_ONLY_TABS`` (currently ``experiments`` — the
cost A/B experiment) are:

  * rendered normally in personal/internal builds,
  * stripped from the served page in opensource builds (render-time strip in
    ``inject_panels`` — nav button AND panel marker, so no dead tab and no
    dangling marker), and
  * excluded from the opensource export, with the runtime flag baked on in
    the exported tree (export.py — belt-and-braces, the same contract as
    lib/mcp/registry.py's ``internal_only`` catalog filter, see
    tests/test_mcp_catalog_internal_only.py).

The backend experiment engine (lib/cost_experiments.py, dispatch metadata,
the report endpoint) deliberately ships in EVERY build: it is disabled by
default (enabled=false), is wired through dispatch/persistence, and the
frontend populate/collect/report functions are already null-safe when the
panel is absent. What is "maintainer-only" is the SETTINGS UI, not the
engine — removing the engine from exports would saw through the dispatch
layer for zero user-visible gain.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import lib.settings_panels as sp

# In an opensource build the experiments fragment is absent and the module
# flag is baked on, so the "rendered in full build" assertions below are not
# applicable. The strip-behavior tests still run (and matter) in both builds.
_OPENSOURCE = sp.is_opensource_build()


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _index_html():
    return _read(os.path.join(PROJECT_ROOT, 'index.html'))


def _fragment(name):
    return _read(os.path.join(
        PROJECT_ROOT, 'static', 'settings_panels', '%s.html' % name))


def _reload_with_flag(monkeypatch, value):
    """Reload lib.settings_panels with TOFU_OPENSOURCE_BUILD set to ``value``.

    Module-global flag is re-read at import time; reload + restore in
    ``finally`` mirrors tests/test_mcp_catalog_internal_only.py exactly.
    """
    if value is None:
        monkeypatch.delenv('TOFU_OPENSOURCE_BUILD', raising=False)
    else:
        monkeypatch.setenv('TOFU_OPENSOURCE_BUILD', value)
    return importlib.reload(sp)


# ── Full build: the tab renders like any other ────────────────────────────

@pytest.mark.skipif(
    _OPENSOURCE,
    reason='maintainer-only tabs are stripped from opensource builds',
)
def test_experiments_tab_rendered_in_full_build():
    html = sp.inject_panels(_index_html())
    assert 'data-tab="experiments"' in html, 'nav button missing'
    assert 'id="settingsTab_experiments"' in html, 'panel missing'
    assert 'settingCostExperimentEnabled' in html, 'A/B controls missing'
    assert '<!-- SETTINGS_PANEL:experiments -->' not in html, (
        'marker must be RESOLVED (spliced), not left in the page'
    )


@pytest.mark.skipif(
    _OPENSOURCE,
    reason='experiments fragment is excluded from opensource builds',
)
def test_ab_experiment_block_lives_in_experiments_fragment():
    """The A/B markup moved OUT of advanced.html into the maintainer-only
    fragment — guards a future edit re-inlining it into a public tab."""
    assert 'settingCostExperimentEnabled' not in _fragment('advanced')
    experiments = _fragment('experiments')
    assert 'id="settingsTab_experiments"' in experiments
    assert 'settingCostExperimentEnabled' in experiments
    assert 'id="costExperimentReport"' in experiments


# ── Opensource build: render-time strip removes every trace ───────────────

def test_experiments_tab_stripped_in_opensource_build(monkeypatch):
    mod = _reload_with_flag(monkeypatch, '1')
    try:
        assert mod.is_opensource_build() is True
        html = mod.inject_panels(_index_html())
        # nav button + marker + panel markup all gone
        assert 'data-tab="experiments"' not in html
        assert 'SETTINGS_PANEL:experiments' not in html
        assert 'settingsTab_experiments' not in html
        assert 'settingCostExperimentEnabled' not in html
        assert 'costExperimentReport' not in html
        # every other tab survives (spot-check the neighbors)
        assert 'data-tab="advanced"' in html
        assert 'id="settingsTab_advanced"' in html
        assert 'id="settingsTab_general"' in html
        # no marker is left dangling for ANY tab
        assert mod.find_markers(html) == []
    finally:
        _reload_with_flag(monkeypatch, None)


def test_strip_patterns_match_real_index_exactly_once():
    """Anti-drift: the strip regexes must each hit the REAL index.html exactly
    once — a restructured button/marker must fail HERE, not silently leak the
    tab into opensource renders."""
    html = _index_html()
    stripped = sp._strip_nav_button(html, 'experiments')
    assert 'data-tab="experiments"' not in stripped
    assert stripped.count('class="settings-tab"') == html.count('class="settings-tab"') - 1
    stripped = sp._strip_panel_marker(html, 'experiments')
    assert 'SETTINGS_PANEL:experiments' not in stripped
    assert stripped.count('SETTINGS_PANEL:') == html.count('SETTINGS_PANEL:') - 1


def test_strip_is_loud_when_index_drifts(monkeypatch, caplog):
    """If index.html drifts so the strip matches nothing, an ERROR must be
    logged (the silent-leak class this mechanism exists to kill)."""
    mod = _reload_with_flag(monkeypatch, '1')
    try:
        with caplog.at_level(logging.ERROR, logger='lib.settings_panels'):
            out = mod.strip_maintainer_only_tabs('<html>no tabs here</html>')
        assert out == '<html>no tabs here</html>'
        errors = [r.getMessage() for r in caplog.records
                  if r.levelno >= logging.ERROR]
        assert any('experiments' in m for m in errors), (
            f'expected a loud drift error, got: {errors}'
        )
    finally:
        _reload_with_flag(monkeypatch, None)


# ── Export pipeline: exclusion + flag bake + forced sanitize candidates ───

def test_export_excludes_experiments_fragment_in_opensource():
    export = pytest.importorskip(
        'export', reason='export.py is not shipped in opensource builds')
    assert 'experiments.html' in export.OPENSOURCE_EXTRA_EXCLUDE_FILES


def test_export_bakes_opensource_flag_in_settings_panels():
    export = pytest.importorskip(
        'export', reason='export.py is not shipped in opensource builds')
    src = _read(os.path.join(PROJECT_ROOT, 'lib', 'settings_panels.py'))
    out = export._sanitize_source_opensource(src, 'lib/settings_panels.py')
    assert "TOFU_OPENSOURCE_BUILD', '1'" in out, (
        'export must bake the opensource flag default in the exported '
        'lib/settings_panels.py (render-time strip defaults ON there)'
    )
    assert "TOFU_OPENSOURCE_BUILD', ''" not in out


def test_opensource_fixed_candidates_cover_flag_bake_files():
    """The flag bake is filepath-gated but the sanitize pass is rg-trigger
    driven — these files contain NO trigger substring, so they MUST be
    force-listed or the bake silently never runs in the exported tree."""
    export = pytest.importorskip(
        'export', reason='export.py is not shipped in opensource builds')
    for rel in ('lib/mcp/registry.py', 'lib/settings_panels.py'):
        assert rel in export._OPENSOURCE_FIXED_CANDIDATES, rel
        content = _read(os.path.join(PROJECT_ROOT, *rel.split('/')))
        assert (
            "_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '')"
            '.strip().lower() in {'
        ) in content, (
            f'{rel} lost the flag line the export bake targets — the bake '
            'would silently become a no-op in exported trees.'
        )
