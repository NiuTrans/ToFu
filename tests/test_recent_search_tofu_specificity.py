"""Root-cause guard (sibling of test_memory_modal_specificity.py): the project
modal's Recent-search input wins its neo-brutalist tofu 2px ink border by NORMAL
cascade — via excluding it at the SOURCE of the over-broad tofu input bleed
(`[data-theme="tofu"] .modal:not(.memory-modal) input:not(.recent-search-input)`)
— NOT by an !important counter-rule.

WHY A TEST
----------
The author's intended rule
    [data-theme="tofu"] .recent-search .recent-search-input {border:2px solid #1a1814}
has specificity (0,3,0). The generic tofu input bleed, before the carve-out, was
    [data-theme="tofu"] .modal:not(.memory-modal) input                 → (0,3,1)
which OUT-SPECIFIES the author's rule and silently flattened the 2px blocky
border to the generic 1px `rgba(184,176,160,0.4)` — the box rendered wrong in
the tofu theme even though the authored rule "looked" correct in the file. The
fix mirrors the sanctioned memory-modal pattern: add `:not(.recent-search-input)`
to the bleed's `input` compound so the bleed no longer matches this input, and
the author's (0,3,0) rule wins by normal cascade. Unproven specificity math is
exactly what regresses when someone later widens the bleed back — this encodes
the invariant instead of asserting it in prose.

jsdom cannot do this (it resolves classList but does not apply the external
stylesheet or compute cascade specificity), so we reuse the REAL CSS specificity
resolver from test_memory_modal_specificity.py against the REAL styles.css.

Four things (mirrors the memory pilot's acceptance criteria):
  1. DELETION — the `.recent-search .recent-search-input` rules carry NO
     !important (the fix is source-exclusion, not a counter-rule).
  2. EXCLUSION IS LOAD-BEARING (NC, on disk) — with the shipped CSS the recent
     input's `border` resolves to the authored `2px solid #1a1814`, NOT the
     bleed's 1px. Neuter: revert `input:not(.recent-search-input)` → `input` on
     the tofu bleed → the bleed (0,3,1) now out-specifies the author (0,3,0) →
     the border flips to the 1px bleed. Restore byte-identical.
  3. SPECIFICITY MATH — encode the (0,3,0) < (0,3,1) relationship that makes the
     source-exclusion (rather than an !important) the correct fix.
  4. NO OVER-NARROWING (control) — a GENERIC tofu modal input (browser/apply
     modal, NOT recent-search) STILL resolves to the tofu bleed, proving the
     `:not()` removed the recent-search input ONLY, not the whole theme.
  5. BASE .modal input BLEED (theme-agnostic, 2026-07-25) — the base
     `.modal input` mega-line rule ((0,1,1): margin-bottom:16px /
     padding:10px 12px / font-size:13px) out-specifies a BARE
     `.recent-search-input` (0,1,0). The 16px bottom margin inflated
     `.recent-search` to 48px and dropped the absolute clear button ~8px
     below the input's true middle (the off-center × bug); the padding
     bleed also killed the 30px right padding that keeps typed text clear
     of the button. Fix: chain the authored selectors to
     `.recent-search .recent-search-input` (0,2,0) — mirrors the
     `.mp-add-row .mp-path-input` pattern. NC: un-chain the base rule on
     disk → padding flips to the bleed's 10px 12px.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

# Reuse the sanctioned CSS specificity engine — single source of truth.
from tests.test_memory_modal_specificity import (  # noqa: E402
    CSS,
    _Elem,
    _css,
    _iter_rules,
    _resolve,
    _selector_matches,
    _specificity,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


# ─────────────────────────── test elements ───────────────────────────

# The Recent-search <input> under the tofu theme, inside the project modal:
#   <div data-theme="tofu"><div class="modal project-modal pm-workbench">
#      … <div class="recent-search"><input class="recent-search-input">
_RECENT_INPUT = _Elem(
    'input', {'recent-search-input'}, theme='tofu',
    ancestors=[{'recent-search'}, {'modal', 'project-modal', 'pm-workbench'}],
)
# A GENERIC tofu modal input (browser/apply modal — NOT recent-search):
#   <div data-theme="tofu"><div class="modal"><input>
_GENERIC_INPUT = _Elem(
    'input', set(), theme='tofu', ancestors=[{'modal'}],
)

_TOFU_INTENDED_BORDER = '2px solid #1a1814'          # authored blocky border
_TOFU_BLEED_BORDER = '1px solid rgba(184,176,160,0.4)'  # the over-broad bleed
_BLEED_SELECTOR = (
    '[data-theme="tofu"] .modal:not(.memory-modal) input:not(.recent-search-input)'
)


@pytest.fixture(scope='module')
def css_text():
    return _css()


# ─────────────────────────── 1. DELETION (no !important) ───────────────────────────

def test_recent_search_rules_have_no_important(css_text):
    """The authored tofu recent-search-input rules must carry NO !important —
    the fix is source-exclusion, not a counter-rule."""
    offenders = []
    for sel, _idx, decls in _iter_rules(css_text):
        if '.recent-search-input' not in sel:
            continue
        if 'data-theme="tofu"' not in sel:
            continue
        for prop, val in decls.items():
            if '!important' in val:
                offenders.append(f'{sel} {{ {prop}: {val} }}')
    assert not offenders, (
        'tofu recent-search-input rules carry !important — the fix should be '
        'the source-exclusion on the bleed, not a counter-rule:\n'
        + '\n'.join(offenders))


# ─────────────────────────── 2. EXCLUSION LOAD-BEARING ───────────────────────────

def test_recent_input_resolves_authored_2px_border(css_text):
    """The recent-search input's border resolves to the authored 2px ink border,
    NOT the generic tofu bleed — proving the source-exclusion works without
    !important."""
    border = _resolve(css_text, _RECENT_INPUT, 'border')
    assert border == _TOFU_INTENDED_BORDER, (
        f'recent-search input border resolved to {border!r}; expected the '
        f'authored {_TOFU_INTENDED_BORDER!r}. If it is the bleed '
        f'{_TOFU_BLEED_BORDER!r} the :not(.recent-search-input) exclusion is '
        f'not protecting it.')
    assert border != _TOFU_BLEED_BORDER


def test_bleed_no_longer_matches_recent_input(css_text):
    """The shipped tofu bleed selector must NOT match the recent-search input
    (that is the whole point of the carve-out)."""
    assert not _selector_matches(_BLEED_SELECTOR, _RECENT_INPUT), (
        'the tofu input bleed still matches the recent-search input — the '
        ':not(.recent-search-input) carve-out is missing or wrong')


# ─────────────────────────── 3. SPECIFICITY MATH ───────────────────────────

def test_specificity_math_authored_would_lose_without_exclusion():
    """Encode the relationship the fix relies on:
    - authored rule  `[data-theme=tofu] .recent-search .recent-search-input` = (0,3,0)
    - base rule      `.modal input`                                          = (0,1,1) → authored wins
    - bleed (no excl)`[data-theme=tofu] .modal:not(.memory-modal) input`      = (0,3,1) → would BEAT
      authored (0,3,0) — hence a plain !important-free authored rule is NOT
      enough; the source-exclusion is required.
    - bleed (shipped, +:not) = (0,4,1) but no longer matches the input at all."""
    authored = _specificity(
        '[data-theme="tofu"] .recent-search .recent-search-input')
    base = _specificity('.modal input')
    bleed_no_excl = _specificity(
        '[data-theme="tofu"] .modal:not(.memory-modal) input')
    bleed_shipped = _specificity(_BLEED_SELECTOR)
    assert authored == (0, 3, 0), authored
    assert base == (0, 1, 1), base
    assert bleed_no_excl == (0, 3, 1), bleed_no_excl
    assert authored > base, 'authored rule must out-specify the base .modal input'
    assert bleed_no_excl > authored, (
        'the un-excluded bleed WOULD out-specify the authored rule (hence the '
        'source-exclusion, not an !important, is the correct fix)')
    assert bleed_shipped == (0, 4, 1), bleed_shipped


# ─────────────────────────── 4. NO OVER-NARROWING (control) ───────────────────────────

def test_generic_tofu_modal_input_still_bleeds(css_text):
    """A generic tofu .modal input (NOT recent-search) STILL resolves to the
    tofu bleed border — the exclusion removed the recent-search input ONLY, not
    the whole theme."""
    border = _resolve(css_text, _GENERIC_INPUT, 'border')
    assert border == _TOFU_BLEED_BORDER, (
        f'generic tofu modal input border resolved to {border!r}; expected the '
        f'tofu bleed {_TOFU_BLEED_BORDER!r}. The :not() over-narrowed and '
        f'stripped the theme from more than the recent-search input.')


# ─────────────────────────── NC double-neuter (on-disk, subprocess) ───────────────────────────

_NC_FIND = (
    '[data-theme="tofu"] .modal:not(.memory-modal) input:not(.recent-search-input){')
_NC_REPL = '[data-theme="tofu"] .modal:not(.memory-modal) input{'


def _subrun_resolve_recent_border() -> str:
    """In a FRESH subprocess, resolve the recent-search input border from the
    CURRENT on-disk styles.css."""
    code = (
        'import tests.test_recent_search_tofu_specificity as t; '
        'print("BORDER=" + str(t._resolve(t._css(), t._RECENT_INPUT, "border")))'
    )
    r = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith('BORDER='):
            return line[len('BORDER='):]
    raise AssertionError(f'subprocess did not report BORDER=: {out}')


def test_nc_reverting_exclusion_reintroduces_bleed():
    """DOUBLE-NEUTER: revert `input:not(.recent-search-input)` → `input` on the
    tofu bleed (ON DISK) → the recent input now inherits the bleed → the border
    resolution flips to the 1px bleed. Restore byte-identical."""
    with open(CSS, encoding='utf-8') as f:
        original = f.read()
    assert original.count(_NC_FIND) == 1, (
        f'NC anchor not unique: count={original.count(_NC_FIND)}')

    # Baseline (shipped): recent input border is the authored 2px, not the bleed.
    base_border = _subrun_resolve_recent_border()
    assert base_border == _TOFU_INTENDED_BORDER, (
        f'baseline not authored border: {base_border!r}')

    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(_NC_FIND, _NC_REPL, 1))
        neut_border = _subrun_resolve_recent_border()
        assert neut_border == _TOFU_BLEED_BORDER, (
            f'NC did not bite: with the exclusion reverted the recent input '
            f'border resolved {neut_border!r}, expected the tofu bleed '
            f'{_TOFU_BLEED_BORDER!r}. The exclusion is not what protects it.')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)

    with open(CSS, encoding='utf-8') as f:
        assert f.read() == original, 'CSS not restored byte-identical after NC'


# ─────────────── 5. BASE .modal input bleed (theme-agnostic) ───────────────

_AUTHORED_PADDING = '6px 30px 6px 12px'    # 30px right padding clears the × button
_BASE_BLEED_PADDING = '10px 12px'          # the .modal input mega-line bleed
_AUTHORED_FONT_SIZE = '12.5px'
_BASE_BLEED_FONT_SIZE = '13px'


def test_base_modal_bleed_loses_padding_and_font_size(css_text):
    """The base `.modal input` bleed (margin-bottom:16px / padding:10px 12px /
    font-size:13px — (0,1,1)) must LOSE to the authored chained rule
    `.recent-search .recent-search-input` (0,2,0). When the selector was bare
    (0,1,0), the bleed's 16px bottom margin inflated .recent-search to 48px
    and dropped the absolute clear button ~8px below the input's true middle
    (the off-center × bug). margin-bottom itself is not resolvable here (the
    engine does not expand the authored `margin: 0` shorthand), so padding and
    font-size stand in as witnesses of the same cascade battle."""
    padding = _resolve(css_text, _RECENT_INPUT, 'padding')
    assert padding == _AUTHORED_PADDING, (
        f'recent-search input padding resolved to {padding!r}; expected the '
        f'authored {_AUTHORED_PADDING!r}. If it is the bleed '
        f'{_BASE_BLEED_PADDING!r} the chained selector lost to the base '
        f'.modal input rule again (the off-center × regression).')
    assert padding != _BASE_BLEED_PADDING
    font_size = _resolve(css_text, _RECENT_INPUT, 'font-size')
    assert font_size == _AUTHORED_FONT_SIZE, (
        f'recent-search input font-size resolved to {font_size!r}; expected '
        f'{_AUTHORED_FONT_SIZE!r} (bleed = {_BASE_BLEED_FONT_SIZE!r})')


def test_specificity_math_chained_selector_beats_base_bleed():
    """Encode the relationship the ×-centering fix relies on:
    - authored `.recent-search .recent-search-input` = (0,2,0)
    - base bleed `.modal input`                      = (0,1,1) → authored wins
    - bare `.recent-search-input` (the pre-fix form) = (0,1,0) → would LOSE."""
    authored = _specificity('.recent-search .recent-search-input')
    base = _specificity('.modal input')
    bare = _specificity('.recent-search-input')
    assert authored == (0, 2, 0), authored
    assert base == (0, 1, 1), base
    assert bare == (0, 1, 0), bare
    assert authored > base, 'chained selector must out-specify the base .modal input'
    assert base > bare, 'the bare selector WOULD lose (that was the off-center × bug)'


_NC2_FIND = '.recent-search .recent-search-input {'
_NC2_REPL = '.recent-search-input {'


def _subrun_resolve_recent_padding() -> str:
    """In a FRESH subprocess, resolve the recent-search input padding from the
    CURRENT on-disk styles.css."""
    code = (
        'import tests.test_recent_search_tofu_specificity as t; '
        'print("PADDING=" + str(t._resolve(t._css(), t._RECENT_INPUT, "padding")))'
    )
    r = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                       capture_output=True, text=True)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.startswith('PADDING='):
            return line[len('PADDING='):]
    raise AssertionError(f'subprocess did not report PADDING=: {out}')


def test_nc_unchaining_selector_reintroduces_base_bleed():
    """DOUBLE-NEUTER: un-chain `.recent-search .recent-search-input` →
    `.recent-search-input` (ON DISK) → the base `.modal input` bleed wins
    again → padding flips to 10px 12px. Restore byte-identical."""
    with open(CSS, encoding='utf-8') as f:
        original = f.read()
    assert original.count(_NC2_FIND) == 1, (
        f'NC-2 anchor not unique: count={original.count(_NC2_FIND)}')

    base_padding = _subrun_resolve_recent_padding()
    assert base_padding == _AUTHORED_PADDING, (
        f'baseline not authored padding: {base_padding!r}')

    try:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original.replace(_NC2_FIND, _NC2_REPL, 1))
        neut_padding = _subrun_resolve_recent_padding()
        assert neut_padding == _BASE_BLEED_PADDING, (
            f'NC-2 did not bite: with the selector un-chained the recent input '
            f'padding resolved {neut_padding!r}, expected the base bleed '
            f'{_BASE_BLEED_PADDING!r}. The chained selector is not what '
            f'protects it.')
    finally:
        with open(CSS, 'w', encoding='utf-8') as f:
            f.write(original)

    with open(CSS, encoding='utf-8') as f:
        assert f.read() == original, 'CSS not restored byte-identical after NC-2'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
