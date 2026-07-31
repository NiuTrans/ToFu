"""Guards for the Project Brain panel styling polish (2026-07-25).

Six defects were found by auditing the `.project-brain-*` / `.pb-*` block in
static/styles.css against the markup it serves (index.html + project-brain.js):

  1. The clamp fade gradient resolved to a HARD-CODED --pb-card-bg, but every
     clamp host (activity row / board card / decision li / influence epic)
     swaps to --pb-card-hover on :hover -> a visible rectangle floated over
     the last line whenever the pointer entered the card.
  2. Only 2 of 4 infinite animations were parked under prefers-reduced-motion.
  3. The (max-width:720px) rule set grid-template-columns on a display:block
     tab host -- inert leftover from the pre-tab 4-column layout -- and put a
     second overflow-y on it, fighting the real scroller.
  4. Charter edit/delete buttons were hover-only (opacity:0;pointer-events:none)
     with no coarse-pointer fallback -> unreachable on touch.
  5. ~20 keyboard-reachable buttons had NO :focus-visible ring.
  6. The 5-tab bar overflows a phone-width panel with an auto-hidden scrollbar
     and no edge cue -> trailing tabs looked absent.

Each test below fails if the corresponding fix is reverted.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / 'static' / 'styles.css').read_text(encoding='utf-8')

pytestmark = pytest.mark.unit


def _rule(selector: str) -> str:
    """Return the declaration body of the first rule exactly matching selector."""
    m = re.search(re.escape(selector) + r'\{([^}]*)\}', CSS)
    return m.group(1) if m else ''


# ── 1. clamp fade follows the host card's current background ──────────────

def test_clamp_fade_uses_indirect_var_not_hardcoded_card_bg():
    body = _rule('.pb-clamp:not(.pb-clamp-open)::after')
    assert body, 'clamp fade rule missing'
    assert '--pb-fade-to' in body, (
        'clamp fade must resolve through --pb-fade-to so hover states can '
        're-point it; a hard-coded --pb-card-bg paints a visible block on hover'
    )
    assert 'var(--pb-fade-to,var(--pb-card-bg))' in body.replace(' ', ''), (
        'the fallback must stay --pb-card-bg for non-hover hosts'
    )


@pytest.mark.parametrize('selector', [
    '.pb-activity-row:hover',
    '.pb-board-card:hover',
    '.pb-charter-decisions li:hover',
    '.pb-inf-epic:hover',
])
def test_every_hover_card_repoints_the_fade(selector):
    body = _rule(selector)
    assert body, f'{selector} rule missing'
    assert 'var(--pb-card-hover)' in body, 'precondition: host swaps background on hover'
    assert '--pb-fade-to:var(--pb-card-hover)' in body.replace(' ', ''), (
        f'{selector} changes its background but does not re-point --pb-fade-to, '
        'so the clamp gradient would fade to the wrong colour'
    )


# ── 2. reduced-motion parks every infinite animation ──────────────────────

def test_reduced_motion_parks_all_infinite_animations():
    start = CSS.index('Project Brain — full visual redesign')
    end = CSS.index('Orchestration Studio')
    blk = CSS[start:end]

    looping = set()
    for m in re.finditer(r'([^{}\n]+)\{[^}]*animation:[^;}]*infinite', blk):
        for sel in m.group(1).split(','):
            looping.add(sel.strip())

    parked = ''
    for m in re.finditer(r'@media[^{]*prefers-reduced-motion[^{]*\{(.*?)\n\}', blk, re.S):
        parked += m.group(1)

    missing = [s for s in looping if s.split(':')[0].split('[')[0] not in parked]
    assert not missing, (
        f'infinite animations not parked under prefers-reduced-motion: {missing}'
    )


# ── 3. no inert grid rule on the display:block tab host ───────────────────

def test_no_dead_grid_rule_on_tab_host():
    assert _rule('.project-brain-columns').startswith('flex:1;display:block'), (
        'precondition: the columns host is a display:block tab host'
    )
    assert 'project-brain-columns{grid-template' not in CSS, (
        'grid-template-columns on a display:block element is inert; it is a '
        'leftover from the pre-tab 4-column layout'
    )


# ── 4. touch devices can reach the charter row actions ────────────────────

def test_charter_row_actions_revealed_on_coarse_pointer():
    base = _rule('.pb-charter-row-actions')
    assert 'opacity:0' in base and 'pointer-events:none' in base, (
        'precondition: the actions are hover-revealed on fine pointers'
    )
    m = re.search(
        r'@media \(hover:none\),\(pointer:coarse\)\{(.*?)\n\}', CSS, re.S)
    assert m, 'no coarse-pointer fallback: buttons unreachable on touch'
    assert '.pb-charter-row-actions' in m.group(1)
    assert 'opacity:1' in m.group(1) and 'pointer-events:auto' in m.group(1)


# ── 5. keyboard focus is visible on the panel's buttons ───────────────────

@pytest.mark.parametrize('cls', [
    '.pb-tab', '.pb-clamp-toggle', '.pb-charter-act', '.pb-board-new',
    '.pb-peer-nudge-send', '.pb-status-refresh', '.pb-watch-btn',
    '.project-brain-close',
])
def test_interactive_control_has_focus_visible_ring(cls):
    sel = '.project-brain-overlay ' + cls + ':focus-visible'
    assert sel in CSS, f'{cls} has no :focus-visible ring — invisible to keyboard users'


def test_focus_ring_uses_outline_not_a_layout_shifting_border():
    """The ring must be an outline (paints outside the box) — a border or a
    box-shadow inset would reflow the control on focus."""
    i = CSS.index('Keyboard focus rings')
    decl = CSS[CSS.index('{', i):CSS.index('}', i)]
    flat = decl.replace(' ', '')
    assert 'outline:2pxsolid' in flat, f'focus ring is not an outline: {decl!r}'
    assert 'outline-offset' in flat
    assert 'border:' not in flat, 'a border would shift layout on focus'


# ── 6. the overflowing tab bar advertises that it scrolls ─────────────────

def test_tab_bar_has_scroll_shadow_cue():
    body = _rule('.project-brain-tabs')
    assert body, 'tab bar rule missing'
    assert 'overflow-x:auto' in body, 'precondition: the bar scrolls'
    assert 'background-attachment:local,local,scroll,scroll' in body.replace(' ', ''), (
        'no Lea-Verou scroll shadow: with auto-hidden overlay scrollbars the '
        'trailing tabs give no hint they exist'
    )
    assert 'radial-gradient' in body, 'edge shadow gradients missing'


def test_tab_bar_hides_its_scrollbar():
    assert 'scrollbar-width:none' in _rule('.project-brain-tabs')
    assert '.project-brain-tabs::-webkit-scrollbar{height:0}' in CSS
