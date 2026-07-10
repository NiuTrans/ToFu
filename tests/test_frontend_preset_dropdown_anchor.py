#!/usr/bin/env python3
"""Model-picker dropdown anchoring invariant (the recurring "对不齐/clipping" class).

THE user-visible objective: the main model picker (`#presetToggle`) is the
LEFT-MOST item in the input toolbar. Its `.preset-dropdown` must open RIGHTWARD
from the toggle's left edge (`left:0; transform:none`). If it is instead
center-anchored on the toggle (`left:50%; transform:translateX(-50%)`), a
~280px-wide model list centered on a far-left toggle spills ~105px past the
toggle's left edge — off the chat panel — where it gets clipped (the reported
bug: "de Opus 4.6" / "Seek V3.2" left-halves cut off in landscape layout).

A bare positional one-liner has no guard against a future edit re-centering it,
so this env-independent CSS-parse test locks the invariant and a NEUTER proves
it actually bites if `translateX(-50%)` is reintroduced.

Asserted on the BASE `.preset-dropdown{…}` rule (not the tofu/media variants):
  1. left-anchored: contains `left:0`.
  2. NOT center-anchored: does NOT contain `translateX(-50%)`.
  3. transform reset: `transform:none` (no leftover shift fighting the anchor).
  4. NC: reintroduce `translateX(-50%)` → assertion (2) must fail.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CSS_PATH = os.path.join(ROOT, 'static', 'styles.css')


def _base_preset_dropdown_rule(css: str) -> str:
    """Return the body of the BASE `.preset-dropdown{…}` rule.

    Anchored on the unique head `.preset-dropdown{display:none` so it can't
    match `.ig-preset-dropdown`, `.preset-dropdown-item`,
    `.preset-dropdown::-webkit-scrollbar`, or `[data-theme="tofu"]
    .preset-dropdown` (those heads differ before/at the brace)."""
    head = '.preset-dropdown{display:none'
    i = css.find(head)
    assert i != -1, 'base .preset-dropdown rule not found in styles.css'
    brace = css.find('{', i)
    end = css.find('}', brace)
    assert end != -1, 'unterminated .preset-dropdown rule'
    return css[brace + 1:end]


def test_preset_dropdown_is_left_anchored():
    """The main model dropdown opens rightward from the toggle's left edge —
    never center-anchored (which overflows off a far-left toggle)."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    body = _base_preset_dropdown_rule(css)

    assert 'left:0' in body, (
        'base .preset-dropdown must be left-anchored (left:0) so a far-left '
        'toggle opens the list rightward into the panel, not off its left edge.')
    assert 'translateX(-50%)' not in body, (
        'base .preset-dropdown must NOT center-anchor (translateX(-50%)): a '
        '~280px list centered on the left-most toolbar toggle spills off the '
        'panel and gets clipped — the reported landscape-clipping bug.')
    assert 'transform:none' in body, (
        'base .preset-dropdown must reset transform to none (no leftover shift '
        'fighting the left anchor during the presetDropIn fade-in).')


def test_NC_preset_dropdown_recentering_bites():
    """NEUTER: re-center the base rule (`left:0`→`left:50%` +
    `transform:none`→`transform:translateX(-50%)`) and confirm the invariant
    FAILS — proving the assertion has teeth and isn't a tautology."""
    css = open(_CSS_PATH, encoding='utf-8').read()
    neutered = css.replace(
        '.preset-dropdown{display:none;position:absolute;bottom:calc(100% + 6px);'
        'left:0;transform:none;',
        '.preset-dropdown{display:none;position:absolute;bottom:calc(100% + 6px);'
        'left:50%;transform:translateX(-50%);',
        1)
    assert neutered != css, 'NC pattern did not match — test is stale'
    body = _base_preset_dropdown_rule(neutered)
    assert 'translateX(-50%)' in body, (
        'neuter did not reintroduce center-anchoring — cannot prove the '
        'invariant bites')
    # The real assertion the positive test makes must now be violated.
    assert not ('left:0' in body and 'translateX(-50%)' not in body), (
        'the left-anchor invariant must FAIL on the re-centered (neutered) CSS')


if __name__ == '__main__':
    test_preset_dropdown_is_left_anchored()
    test_NC_preset_dropdown_recentering_bites()
    print('PASS both')
