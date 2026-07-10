#!/usr/bin/env python3
"""Coarse-pointer model-picker dropdown must ESCAPE the clipped .chat-wrapper.

Regression context (2026-07-09): the blank-screen fix set
`[data-theme="tofu"] .chat-wrapper{overflow:hidden!important}` inside the
tablet-drawer block `@media(min-width:769px) and (max-width:1024px) and
(pointer:coarse)`. But the model-picker `.preset-dropdown` is a DOM descendant
of `.chat-wrapper` (chat-wrapper → input-area → … → preset-toggle-wrapper →
preset-dropdown) and, in its DESKTOP form, opens UPWARD with
`position:absolute; bottom:calc(100%+6px)`. On desktop the tofu theme keeps
`.chat-wrapper{overflow:visible}` as a "dropdown-escape hatch" so it isn't
clipped — but the coarse/tablet block now clips it, so tapping the model
toggle showed NOTHING (dropdown rendered but clipped away).

The phone block (`@media(max-width:768px)`) already dodges this by making
`.preset-dropdown` a `position:fixed` bottom-sheet (escapes any ancestor
overflow). The tablet-coarse band had NO such rule → the bug.

Invariants locked here (env-independent CSS parse):
  1. The tablet-coarse block (769–1024, pointer:coarse) contains a
     `.preset-dropdown` rule that is `position:fixed` (escapes the clip).
  2. Any coarse/mobile bottom-sheet `.preset-dropdown` max-height is sized off
     the `--vh100` guard var (or `vh`), NOT bare `dvh` — the same WebView
     collapsed-ICB class of bug fixed for the 11 overlays (`dvh` → 0).
  3. NEUTER: removing the fixed-positioning from the coarse block re-exposes
     the clip → assertion (1) fails.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')


def _media_block(css: str, opener_contains: list[str],
                 body_contains: str | None = None) -> str:
    """Return the body of the @media block whose opener contains ALL
    `opener_contains` substrings AND (if given) whose body contains
    `body_contains`. Brace-matched so nested rules are included.

    The disambiguator matters: several @media blocks share the exact opener
    `(min-width:769px) and (max-width:1024px) and (pointer:coarse)` (paper-mode,
    reading-mode, and the chat drawer). We want the CHAT DRAWER block, which is
    uniquely identified by its `.chat-wrapper{overflow:hidden` clip rule."""
    for m in re.finditer(r'@media[^{]*\{', css):
        opener = m.group(0)
        if not all(s in opener for s in opener_contains):
            continue
        i = m.end() - 1
        depth = 0
        for j in range(i, len(css)):
            if css[j] == '{':
                depth += 1
            elif css[j] == '}':
                depth -= 1
                if depth == 0:
                    body = css[i + 1:j]
                    if body_contains is None or body_contains in body:
                        return body
                    break
    raise AssertionError(
        f'@media block containing {opener_contains} '
        f'(body~{body_contains!r}) not found')


def test_coarse_preset_dropdown_escapes_clipped_chat_wrapper():
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _media_block(
        css, ['min-width:769px', 'max-width:1024px', 'pointer:coarse'],
        body_contains='.chat-wrapper{overflow:hidden')

    # There must be a .preset-dropdown rule in this coarse band…
    m = re.search(r'\.preset-dropdown\s*\{([^}]*)\}', block)
    assert m, (
        'the tablet-coarse (769–1024, pointer:coarse) @media block must define '
        'a .preset-dropdown rule so the model-picker dropdown escapes the '
        'clipped .chat-wrapper; without it the desktop absolute-upward dropdown '
        'is clipped and tapping the toggle shows nothing.')
    body = m.group(1)
    # …and it must be position:fixed (escapes ANY ancestor overflow:hidden).
    assert 'position:fixed' in body, (
        'coarse .preset-dropdown must be position:fixed (a bottom-sheet) to '
        'escape the .chat-wrapper{overflow:hidden} clip introduced by the '
        'blank-screen fix in this same block.')


def test_coarse_and_phone_bottom_sheet_maxheight_not_bare_dvh():
    """Any fixed bottom-sheet preset-dropdown must not size height off bare
    `dvh` (collapses to 0 in the WebView) — use --vh100 (or vh)."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    targets = [(['max-width:768px'], None),
               (['min-width:769px', 'max-width:1024px', 'pointer:coarse'],
                '.chat-wrapper{overflow:hidden')]
    for opener, body_marker in targets:
        try:
            block = _media_block(css, opener, body_contains=body_marker)
        except AssertionError:
            continue
        for m in re.finditer(r'\.preset-dropdown\s*\{([^}]*)\}', block):
            body = m.group(1)
            if 'position:fixed' not in body:
                continue
            mh = re.search(r'max-height:\s*([^;]+)', body)
            if not mh:
                continue
            val = mh.group(1)
            assert 'dvh' not in val or '--vh100' in val, (
                f'fixed bottom-sheet .preset-dropdown max-height={val!r} in '
                f'@media {opener} uses bare dvh, which collapses to 0 in the '
                f'Android WebView (same class as the 11-overlay fix); size it '
                f'off var(--vh100) instead.')


def test_coarse_toggle_cannot_collapse_to_a_sliver():
    """The model picker must stay visible at coarse/tablet width.

    The tablet-drawer block keeps the FULL desktop toolbar, so submenus +
    actions + search + send share the nowrap row. The model picker is fully
    shrinkable (.input-actions-scroll → #modelGroup{flex-shrink:1} →
    .preset-toggle-wrapper → .preset-toggle{overflow:hidden;min-width:0}), so
    without relief it collapses to an invisible sliver (NOT display:none).

    Invariant: the coarse block must EITHER compact the row (hide the submenus,
    like the phone block) OR pin the toggle against shrink. We assert BOTH the
    compaction (submenus hidden) and a non-shrinking toggle floor.
    """
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _media_block(
        css, ['min-width:769px', 'max-width:1024px', 'pointer:coarse'],
        body_contains='.chat-wrapper{overflow:hidden')
    norm = block.replace(' ', '').replace('\n', '')

    # Compaction: the three submenus are hidden in this block.
    assert '#submenuAI' in block and 'display:none' in block, (
        'coarse block must hide the submenus (#submenuAI/Tools/Mode) so the row '
        'has width for the model picker — else it collapses to a sliver.')

    # Toggle floor: .preset-toggle is pinned against shrink (flex-shrink:0 or a
    # real min-width), so it can never be squeezed to zero width.
    has_shrink0 = '.preset-toggle{flex-shrink:0' in norm or \
                  '#modelGroup{flex-shrink:0' in norm
    has_minwidth = bool(re.search(r'\.preset-toggle\{[^}]*min-width:\d', norm))
    assert has_shrink0 or has_minwidth, (
        'coarse .preset-toggle / #modelGroup must resist flex-shrink '
        '(flex-shrink:0 or a min-width floor) so the model picker stays visible.')


def test_NC_coarse_toggle_floor_bites():
    """NEUTER: strip BOTH relief mechanisms (submenu compaction + toggle floor)
    from the coarse block → the visibility invariant must fail."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _media_block(
        css, ['min-width:769px', 'max-width:1024px', 'pointer:coarse'],
        body_contains='.chat-wrapper{overflow:hidden')
    # Remove submenu compaction and both toggle-floor forms.
    neutered = re.sub(r'#submenuAI[^}]*\{[^}]*display:none[^}]*\}', '', block)
    neutered = neutered.replace('flex-shrink:0', 'flex-shrink:1')
    neutered = re.sub(r'(\.preset-toggle\{[^}]*?)min-width:\d+px;?', r'\1', neutered)
    norm = neutered.replace(' ', '').replace('\n', '')

    compaction = ('#submenuAI' in neutered and 'display:none' in neutered)
    shrink0 = '.preset-toggle{flex-shrink:0' in norm or '#modelGroup{flex-shrink:0' in norm
    minwidth = bool(re.search(r'\.preset-toggle\{[^}]*min-width:\d', norm))
    has_relief = compaction or shrink0 or minwidth
    assert not has_relief, (
        'NEUTER must bite: with submenu compaction + toggle floor stripped, the '
        'coarse block has no relief and the model picker can collapse — the '
        'visibility invariant should have nothing left to assert.')


def test_NC_coarse_escape_bites():
    """NEUTER: strip position:fixed from the coarse .preset-dropdown → the
    escape invariant must fail."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    block = _media_block(
        css, ['min-width:769px', 'max-width:1024px', 'pointer:coarse'],
        body_contains='.chat-wrapper{overflow:hidden')
    m = re.search(r'\.preset-dropdown\s*\{([^}]*)\}', block)
    assert m, 'coarse .preset-dropdown missing — positive test is stale'
    neutered_body = m.group(1).replace('position:fixed', 'position:absolute')
    assert 'position:fixed' not in neutered_body, 'neuter no-op — stale'
    # Emulate the positive assertion on the neutered body: it must now fail.
    assert 'position:fixed' not in neutered_body  # sanity
    ok = 'position:fixed' in neutered_body
    assert not ok, 'the escape invariant must FAIL when position:fixed removed'


if __name__ == '__main__':
    test_coarse_preset_dropdown_escapes_clipped_chat_wrapper()
    test_coarse_and_phone_bottom_sheet_maxheight_not_bare_dvh()
    test_NC_coarse_escape_bites()
    print('PASS')
