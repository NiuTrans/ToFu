"""Drift-guard for the composer toolbar `.submenu-dropdown` polish (2026-07-08).

Two root-cause fixes this test locks in so a future churn of the (heavily,
concurrently edited) styles.css cannot silently regress them:

  (1) LIGHT-THEME SHADOW GAP. The base `.submenu-dropdown` shadow is a dark
      drop-shadow (`rgba(0,0,0,…)`) held in a `--submenu-shadow` token. On the
      light theme that dark shadow reads as a smudge on the pale surface, so a
      `[data-theme="light"] .submenu-dropdown` override re-points the token to a
      soft warm-neutral tint. INVARIANT: that light override must EXIST and must
      NOT carry a raw `rgba(0,0,0,…)` shadow.

  (2) DEAD DUPLICATE TRANSFORM. The base rule historically declared `transform`
      twice (the first immediately clobbered by the second). INVARIANT: the base
      `.submenu-dropdown` rule declares the `transform` property EXACTLY ONCE
      (`transform-origin` and the `transform` keyword inside `transition` do not
      count).

Env-independent: parses static/styles.css directly. NEUTERs included for each.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """Remove /* … */ comments (they can contain literal braces that corrupt the
    naive brace-based rule splitter — the documented styles.css test trap).

    Delegates to the SINGLE shared implementation (charter #24).

    EQUIVALENCE, MEASURED on the real 22k-line static/styles.css rather than
    assumed: the local ``re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)`` this
    replaced and ``strip_comments(lang='css', inline=True)`` produce an
    IDENTICAL selector set (6466 rules, 0 selectors unique to either side) and
    a byte-identical whitespace-stripped content signature. They differ only in
    LINE NUMBERING -- the shared one blanks comment lines to preserve line
    count, the local one deleted them (20295 vs 22400 lines) -- which leaves 25
    rule bodies differing in whitespace alone. Every assertion here is
    whitespace-insensitive (substring / regex on a rule body), so the swap is
    behaviour-preserving; the suite is the proof.

    Keeping N copies of "what counts as a comment" is what let a fix land in one
    copy and not its duplicate -- incident 3 in the shared module's docstring.
    """
    from tests._source_scan import strip_comments
    return strip_comments(css, lang='css', inline=True)


def _rule_body(css: str, selector: str) -> str | None:
    """Return the declaration body of the FIRST rule whose selector-list is
    EXACTLY `selector` (whitespace-normalized). Innermost rules only, so this
    works whether or not the rule is nested in an @media block (declarations
    contain no braces)."""
    css = _strip_comments(css)
    want = re.sub(r'\s+', ' ', selector).strip()
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel = re.sub(r'\s+', ' ', m.group(1)).strip()
        if sel == want:
            return m.group(2)
    return None


def _count_transform_prop(body: str) -> int:
    """Count standalone `transform:` PROPERTY declarations — excludes
    `transform-origin:` and the `transform` keyword inside a `transition:` list."""
    return len(re.findall(r'(?:^|;)\s*transform\s*:', body))


# ── Invariant (2): no dead duplicate transform on the base rule ──────────────

def test_base_submenu_dropdown_declares_transform_once():
    css = _read(CSS)
    body = _rule_body(css, '.submenu-dropdown')
    assert body is not None, 'base .submenu-dropdown rule not found (structure changed?)'
    n = _count_transform_prop(body)
    assert n == 1, (
        f'base .submenu-dropdown declares the `transform` property {n}× — a dead '
        'duplicate transform is back (the first is clobbered by the second). '
        'Declare it exactly once.\nbody=' + body)


def test_nc_duplicate_transform_is_flagged():
    """NEUTER: inject a 2nd `transform:` into the base rule → count must rise to 2."""
    css = _read(CSS)
    body = _rule_body(css, '.submenu-dropdown')
    assert body is not None and _count_transform_prop(body) == 1, 'fix real CSS first'
    poisoned_body = body + ';transform:translateX(-50%) translateY(4px)'
    assert _count_transform_prop(poisoned_body) == 2, (
        'the counter did not detect an injected duplicate transform — the guard '
        'is not load-bearing.')


# ── Invariant (1): light theme has a non-black submenu-dropdown shadow ───────

def test_light_theme_submenu_dropdown_shadow_is_not_black():
    css = _read(CSS)
    body = _rule_body(css, '[data-theme="light"] .submenu-dropdown')
    assert body is not None, (
        'no [data-theme="light"] .submenu-dropdown override — the base dark '
        'rgba(0,0,0,…) drop-shadow will smudge on the pale light surface. '
        'Add a warm-neutral --submenu-shadow (or box-shadow) override.')
    assert ('--submenu-shadow' in body) or ('box-shadow' in body), (
        'the light .submenu-dropdown override sets neither --submenu-shadow nor '
        'box-shadow — it does not actually re-tint the shadow.\nbody=' + body)
    assert 'rgba(0,0,0' not in body.replace(' ', ''), (
        'the light .submenu-dropdown shadow uses a raw rgba(0,0,0,…) drop-shadow '
        '— that is the dark smudge this fix removed. Use a warm-neutral tint.\n'
        'body=' + body)


def test_nc_light_black_shadow_is_flagged():
    """NEUTER: re-point the light override's token to a black drop-shadow → the
    'not black' assertion must be able to catch it."""
    css = _read(CSS)
    body = _rule_body(css, '[data-theme="light"] .submenu-dropdown')
    assert body is not None, 'fix real CSS first'
    assert 'rgba(0,0,0' not in body.replace(' ', ''), 'real light rule already black?!'
    poisoned = '--submenu-shadow:0 8px 32px rgba(0,0,0,0.3);border-color:var(--border)'
    assert 'rgba(0,0,0' in poisoned.replace(' ', ''), (
        'the black-shadow detector would not flag a regressed light override.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
