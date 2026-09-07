"""tests/test_browser_extension_popup_design.py — popup redesign guards.

WHY
---
2026-08-04 owner directive: the extension popup read as a leftover of the
dark era — purple-on-navy while every panel in the app had moved to the
cream/ink/gold tofu block, a flat information hierarchy (the rarely-used
Pause button outweighed the one question "is it working?"), and emoji
standing in for iconography (title spark, ID glyph, pause/resume symbols,
four stat glyphs). The redesign pins:

  1. PALETTE — the popup carries the tofu tokens (cream #f2ecda, paper
     #faf8f3, ink #1a1814, gold #c9993f) and the dark-era hexes never
     come back;
  2. NO EMOJI — zero emoji in popup.html / popup.js user-visible strings;
     status is a CSS dot, stats are numbered tiles, controls are words;
  3. HIERARCHY — DOM order is the reading order: status hero → repair
     remedy → server field → advanced → stats → footer controls;
  4. STRUCTURE — every id popup.js wires against exists in popup.html
     (the drift class where JS updates an element that was renamed);
  5. NO KEYBOARD FIGHTS — the 2s poll refresh writes an input only
     through the focus/dirty-guarded writer (owner review 2026-08-04:
     an unconditional write clobbered a URL being typed every tick).

The repair-row / details / version-badge pins live in
test_browser_bridge_auto_repair.py and
test_chrome_store_manifest_parity.py — this file deliberately does not
re-pin them; it owns the DESIGN contract only.
"""

import os
import re

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as f:
        return f.read()


# ── 1. Palette: tofu block, never the dark-era purple/navy ───────────

_TOFU_TOKENS = ('#f2ecda', '#faf8f3', '#1a1814', '#c9993f')
_DARK_ERA_HEXES = ('#1a1a2e', '#a78bfa', '#16213e', '#0f0f23', '#8b5cf6')


def test_popup_carries_the_tofu_palette():
    html = _src('browser_extension/popup.html')
    for token in _TOFU_TOKENS:
        assert token in html, (
            f'tofu token {token} missing — the popup drifted off the app '
            f'design language (cream chrome / ink borders / gold accents)')


def test_dark_era_hexes_never_come_back():
    html = _src('browser_extension/popup.html')
    for hexcode in _DARK_ERA_HEXES:
        assert hexcode not in html, (
            f'dark-era hex {hexcode} is back — the popup was the last '
            f'purple-on-navy surface; the tofu block replaced it wholesale')


# ── 2. No emoji — iconography is CSS, controls are words ─────────────

# Emoji + pictograph ranges. Excludes general punctuation (— … · • are
# typography, not emoji). Covers the media-control block (⏸ U+23F8,
# ⏳ U+23F3), geometric shapes (▶ U+25B6), misc symbols + dingbats
# (✓ U+2713, ✦ U+2726), supplemental arrows, and all SMP pictographs
# (🆔 U+1F194, 📤 U+1F4E4).
_EMOJI_RE = re.compile(
    '[⌀-⏿■-➿⬀-⯿\U0001F000-\U0001FAFF]')


def test_no_emoji_anywhere_in_the_popup():
    for rel in ('browser_extension/popup.html', 'browser_extension/popup.js'):
        src = _src(rel)
        hits = _EMOJI_RE.findall(src)
        assert not hits, (
            f'{rel} contains emoji {hits[:6]} — the 2026-08-04 redesign '
            f'replaced every glyph with a CSS shape or a plain word; '
            f'emoji read as unprofessional in the owner review')


# ── 3. Hierarchy: DOM order IS the reading order ──────────────────────

def test_dom_order_is_status_repair_server_stats_controls():
    html = _src('browser_extension/popup.html')
    order = [
        'id="statusDot"',      # hero — the one question: is it working?
        'id="repairRow"',      # the ONE remedy, when the credential died
        'id="serverUrl"',      # occasional configuration
        'id="bridgeSecret"',   # collapsed advanced escape hatch
        'id="stats"',          # informational telemetry
        'id="toggleBtn"',      # rare control
        'id="clientIdText"',   # diagnostic detail
    ]
    positions = [html.index(anchor) for anchor in order]
    assert positions == sorted(positions), (
        'popup DOM order drifted — the hierarchy is status → repair → '
        'server → advanced → stats → controls; a flat layout was one of '
        'the two defects the redesign fixed')


# ── 4. Structure: every id the JS wires against exists ────────────────

def test_every_id_popup_js_wires_exists_in_the_html():
    html = _src('browser_extension/popup.html')
    js = _src('browser_extension/popup.js')
    wired = sorted(set(re.findall(r"getElementById\('([A-Za-z]+)'\)", js)))
    missing = [i for i in wired if f'id="{i}"' not in html]
    assert not missing, (
        f'popup.js wires {missing} but popup.html has no such id — the '
        f'redesign renamed an element without updating the other half')


def test_status_states_have_distinct_dot_classes():
    """Connected / paused / disconnected are three different questions and
    must not collapse into one grey dot — 'paused by me' used to render as
    'Disconnected', which reads as a fault."""
    js = _src('browser_extension/popup.js')
    for state in ('connected', 'paused', 'disconnected'):
        assert f"'{state}'" in js, (
            f'popup.js lost the {state!r} status state')
    html = _src('browser_extension/popup.html')
    for cls in ('status-dot.connected', 'status-dot.paused', 'status-dot.disconnected'):
        assert cls in html, (
            f'popup.html lost the .{cls} style — the state renders naked')


def test_stats_render_as_numbered_tiles_not_glyph_lines():
    js = _src('browser_extension/popup.js')
    assert 'statTile(' in js and 'stat-num' in js and 'stat-label' in js, (
        'stats must render as number-over-label tiles — the old '
        'glyph-prefixed lines were the emoji defect')


# ── 5. The poll never fights the user's keyboard ─────────────────────

def test_poll_refresh_never_overwrites_an_edited_field():
    """Owner review 2026-08-04: the 2s ``setInterval`` refresh wrote
    ``serverInput.value = resp.serverUrl`` unconditionally — a user typing
    a new server URL was clobbered every tick. Auto-refreshed fields go
    through a focus/dirty-guarded writer, and every future refreshed
    field must ride the same gate."""
    js = _src('browser_extension/popup.js')
    assert not re.search(r'serverInput\.value\s*=[^=]', js), (
        'the poll writes the server URL field unconditionally again — '
        'a user edit in progress is overwritten every 2s tick')
    assert 'activeElement' in js and 'dirty' in js, (
        'the focus/dirty gate is gone — the poll can overwrite an edit '
        'in progress')
    assert "addEventListener('input'" in js, (
        'the dirty latch must be set by the user’s first keystroke — '
        'without it only focus protects the field, and a blur re-exposes it')
    assert 'guardedField(' in js, (
        'the guarded writer is the reusable embodiment of the rule — a '
        'one-off inline check will not be copied to the next refreshed field')


# ── 6. NEUTER — prove the pins bite ───────────────────────────────────

def test_NEUTER_dropping_a_tofu_token_is_caught():
    html = _src('browser_extension/popup.html')
    neutered = html.replace('#c9993f', '#c9993e')
    assert neutered != html, 'NEUTER did not touch the gold token'
    assert not all(t in neutered for t in _TOFU_TOKENS), (
        'sanity: with gold gone the palette pin above goes red')


def test_NEUTER_an_emoji_sneaking_back_is_caught():
    src = _src('browser_extension/popup.html')
    assert _EMOJI_RE.search(src + '⏸'), (
        'sanity: the emoji scanner must match the glyphs the redesign '
        'removed (pause symbol)')
    assert _EMOJI_RE.search('✓ Saved'), (
        'sanity: the emoji scanner must match the old Saved glyph')


def test_NEUTER_unconditional_field_write_is_caught():
    js = _src('browser_extension/popup.js')
    neutered = js.replace('serverField.refresh(resp.serverUrl)',
                          'serverInput.value = resp.serverUrl')
    assert neutered != js, 'NEUTER did not restore the unconditional write'
    assert re.search(r'serverInput\.value\s*=[^=]', neutered), (
        'sanity: the poll-vs-keyboard pin above goes red on the old shape')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
