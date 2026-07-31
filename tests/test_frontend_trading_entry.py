"""Guards for the trading entry point + the relocated update button.

Two changes this covers, both in the "silent no-op" family the other frontend
guards exist for:

1. **Trading entry.** The trading module is an optional plugin whose backend is
   gated per request (``tofu_trading/gate.py``). Its topbar entry must be
   flag-driven and must apply WITHOUT a reload, mirroring the existing
   ``_applyDebugModeVisibility`` pattern. A hardcoded-visible button would
   advertise a feature that 404s; an entry that only updates on reload would
   look broken right after toggling.

2. **Update button relocation.** Moved from the always-visible topbar into
   Settings › General. ``update.js`` still drives its "New" pill from the boot
   check, so the settings surface must be fed by the SAME state
   (``_renderSettingsUpdatePill``) rather than re-deriving availability.

These assert on the shipped markup/wiring rather than on rendered pixels — the
failure mode being guarded is a missing wire, not a layout change.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_frontend_trading_entry.py -v
"""
from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def _index():
    return _read('index.html')


def _general_panel():
    return _read('static', 'settings_panels', 'general.html')


# ── Trading entry point ───────────────────────────────────────────────

def test_trading_entry_sits_with_the_other_feature_launchers():
    """The entry belongs in the topbar tool group, next to Paper."""
    html = _index()
    assert 'id="tradingModeBtn"' in html, 'trading entry button is missing'
    tools = re.search(r'<div class="topbar-tools".*?</div>\s*</div>', html, re.S)
    assert tools and 'tradingModeBtn' in tools.group(0), (
        'the trading entry must live inside .topbar-tools with the other launchers'
    )
    paper = html.index('id="paperModeBtn"')
    trading = html.index('id="tradingModeBtn"')
    assert trading > paper, 'trading entry should follow the Paper launcher'


def test_trading_entry_starts_hidden():
    """Default-hidden: the flag decides, so a flag fetch that never lands must
    not leave a dead button pointing at a 404 page."""
    html = _index()
    btn = re.search(r'<button[^>]*id="tradingModeBtn"[^>]*>', html)
    assert btn, 'trading entry button not found'
    assert 'display:none' in btn.group(0), (
        'the trading entry must default to hidden and be revealed by the flag'
    )


def test_trading_visibility_is_flag_driven_and_applied_at_boot():
    html = _index()
    assert '_applyTradingVisibility' in html
    fn = re.search(
        r'window\._applyTradingVisibility\s*=\s*function\s*\(\)\s*\{(.*?)\n\};',
        html, re.S)
    assert fn, '_applyTradingVisibility helper not found'
    body = fn.group(1)
    assert 'trading_enabled' in body, 'visibility must read the trading_enabled flag'
    assert 'tradingModeBtn' in body, 'visibility must target the entry button'
    # Applied on the boot flag-load path, or the button stays hidden until a
    # settings save happens to run.
    loader = re.search(r'async function loadFeatureFlags\(\).*?\}\)\(\);', html, re.S)
    assert loader and '_applyTradingVisibility' in loader.group(0), (
        'loadFeatureFlags must apply trading visibility so the entry appears '
        'on a normal page load'
    )


def test_toggling_trading_applies_without_a_reload():
    """The settings save path must re-apply visibility, like the debug toggle."""
    js = _read('static', 'js', 'settings', 'save_export.js')
    block = re.search(r'trading_enabled.*?\n\s*\}\n\s*\}', js, re.S)
    assert block, 'trading toggle save block not found'
    # Assert the CALL, not a mention: the surrounding comment names the helper
    # too, so a substring check passed even with the call removed (verified by
    # neutering it).
    assert re.search(r'window\._applyTradingVisibility\s*\(\s*\)', block.group(0)), (
        'saving the trading toggle must CALL window._applyTradingVisibility(), '
        'otherwise the change only appears after a manual reload'
    )


def test_no_restart_hint_remains_for_the_trading_toggle():
    """The flag is hot now (request-time gate + per-pass worker checks), so any
    surviving "restart required" affordance would be a false statement.

    It was false even before: nothing read the flag at all, so the promised
    restart applied a change that never happened.
    """
    for content, where in (
        (_index(), 'index.html'),
        (_general_panel(), 'settings_panels/general.html'),
        (_read('static', 'js', 'settings', 'core_panel.js'), 'core_panel.js'),
        (_read('static', 'js', 'settings', 'save_export.js'), 'save_export.js'),
        (_read('static', 'js', 'i18n.js'), 'i18n.js'),
    ):
        assert 'tradingRestartHint' not in content, (
            f'{where} still references the removed restart hint')
        assert 'settings.tradingRestart' not in content, (
            f'{where} still references the removed restart i18n key')


# ── Update button relocation ──────────────────────────────────────────

def test_update_entry_lives_in_settings():
    panel = _general_panel()
    assert 'id="settingsUpdateBtn"' in panel, 'settings update button missing'
    assert 'openUpdateDialog()' in panel, (
        'the settings update button must open the existing update dialog '
        'rather than reimplementing the flow')
    assert 'id="settingsUpdatePill"' in panel, 'update "New" pill missing'


def test_topbar_update_button_is_no_longer_visible_chrome():
    """The stub stays (update.js targets #updateBtn) but must not render."""
    html = _index()
    btn = re.search(r'<button[^>]*id="updateBtn"[^>]*>', html)
    assert btn, (
        '#updateBtn must remain as a hidden stub — update.js\'s boot check '
        'writes the availability state onto it')
    assert 'display:none' in btn.group(0), (
        'the topbar update button should no longer occupy permanent chrome')


def test_update_pill_has_one_source_of_truth():
    """Availability is computed once in update.js and mirrored, not re-derived."""
    upd = _read('static', 'js', 'update.js')
    assert 'function _renderSettingsUpdatePill' in upd, (
        'update.js should own the settings pill rendering')
    badge = re.search(r'function _renderUpdateBadge\(\)\s*\{(.*?)\n\}', upd, re.S)
    assert badge and '_renderSettingsUpdatePill' in badge.group(1), (
        'the topbar badge renderer must also refresh the settings pill, so the '
        'two surfaces cannot disagree about whether an update exists')

    core = _read('static', 'js', 'settings', 'core_panel.js')
    assert 'classList.contains(\'has-update\')' not in core, (
        'core_panel.js must not re-derive update availability from the DOM; '
        'call _renderSettingsUpdatePill instead')


def test_update_card_is_styled():
    """The card must not ship unstyled — every class it uses needs a rule."""
    panel = _general_panel()
    css = _read('static', 'styles.css')
    used = set(re.findall(r'class="(stg-update-[a-z-]+)"', panel))
    assert used, 'no stg-update-* classes found in the panel markup'
    missing = sorted(c for c in used if f'.{c}' not in css)
    assert not missing, f'update card classes with no CSS rule: {missing}'


def test_update_i18n_keys_cover_both_languages():
    i18n = _read('static', 'js', 'i18n.js')
    for key in ('settings.aboutUpdate', 'settings.updateTitle',
                'settings.updateDesc', 'settings.updateCheck',
                'settings.updateCurrent', 'topbar.trading'):
        # Match to end-of-line rather than to the first '}': an interpolated
        # value like '当前版本 v{version}' contains a brace of its own.
        entry = re.search(re.escape(f"'{key}'") + r':\s*\{(.*)$', i18n, re.M)
        assert entry, f'missing i18n key: {key}'
        body = entry.group(1)
        assert 'zh:' in body and 'en:' in body, (
            f'{key} must define both zh and en')
