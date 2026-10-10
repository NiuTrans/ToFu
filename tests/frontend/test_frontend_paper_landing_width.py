"""Drift-guard for the paper-reader landing action-column width.

Root cause it locks in: the landing / upload card column
``.paper-landing-actions`` was hard-capped at ``max-width:400px`` regardless of
the PDF pane width. In a wide desktop pane that stranded the card between large
side-gutters AND squeezed the arxiv search ``<input>`` (which shares its row
with the 搜索 button and reserves ~36px of left padding for the search icon)
too narrow to render its full placeholder. The 2026-07-08 fix raised the cap to
520px; 2026-09-10 raised it again to 640px (direct-PDF-link placeholders and the
wider 搜索 / 获取 button needed the extra room).

Two coupled defects locked in here, both invisible to a stylesheet parse until
a real browser laid the landing out (2026-09-11 probe):

1. ``.paper-landing`` used ``align-items:center`` (flex column) with no
   ``align-self``, so it SHRANK to its content width (~386px) inside its ~496px
   ``.paper-pdf-container`` parent instead of filling it — the action column
   was doubly squeezed regardless of any ``max-width``. The fix adds
   ``align-self:stretch`` so the landing fills the PDF pane.

2. ``.paper-landing-actions`` then stretches to that pane width, CAPPED by
   ``max-width`` so a very wide pane doesn't produce an absurdly long card.
   Because the PDF pane is a user-draggable ~50% split, the cap is an upper
   safety bound, not the rendered width: at a 1000px pane the column renders
   ~408px. The guard therefore pins (a) ``align-self:stretch`` on
   ``.paper-landing`` and (b) a ``max-width`` cap >= 640px on
   ``.paper-landing-actions`` so a later responsive edit can't silently
   re-shrink either — re-clipping the placeholder with NO error.

Env-independent: parses the RETAINED stylesheet
``frontend/src/styles/application/03-paper-reader.css`` (the source
``npm run build:frontend`` compiles into static/styles.css — the AGENTS.md
rule: never edit or assert on the generated bundle). A NEUTER reverts the cap
to 400px and confirms the assertion would fire.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
CSS = os.path.join(
    ROOT, 'frontend', 'src', 'styles', 'application', '03-paper-reader.css')

MIN_WIDTH_PX = 640


def _decl(body: str, prop: str) -> str | None:
    """Return the LAST ``prop:value`` declaration value in a rule body, or
    None if absent (later declarations win, matching CSS cascade)."""
    vals = re.findall(re.escape(prop) + r'\s*:\s*([^;}]+)', body)
    return vals[-1].strip() if vals else None


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """Remove /* … */ comments (they can contain literal braces that corrupt the
    naive brace-based rule splitter — the documented styles.css test trap).

    Delegates to the SINGLE shared implementation (charter #24).

    EQUIVALENCE, MEASURED on the real 22k-line static/styles.css rather than
    assumed: the local ``re.sub(r'/\\*.*?\\*/', '', css, flags=re.DOTALL)`` this
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
    works whether or not the rule is nested in an @media block."""
    css = _strip_comments(css)
    want = re.sub(r'\s+', ' ', selector).strip()
    for m in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        sel = re.sub(r'\s+', ' ', m.group(1)).strip()
        if sel == want:
            return m.group(2)
    return None


def _max_width_px(body: str) -> int | None:
    """Extract the integer px value of the LAST `max-width:<N>px` in a rule body
    (later declarations win). Returns None if absent or non-px."""
    vals = re.findall(r'max-width\s*:\s*(\d+)px', body)
    return int(vals[-1]) if vals else None


def test_landing_stretches_to_fill_the_pane():
    """.paper-landing must keep ``align-self:stretch`` — without it the flex
    ``align-items:center`` shrink-wraps the landing to its content, and the
    action column inside can never fill the PDF pane no matter how large its
    own ``max-width`` is (the 2026-09-11 live-probe root cause)."""
    body = _rule_body(_read(CSS), '.paper-landing')
    assert body is not None, '.paper-landing rule not found (structure changed?)'
    assert _decl(body, 'align-self') == 'stretch', (
        '.paper-landing lost align-self:stretch — the landing shrink-wraps to '
        'its content again and the action column is re-clipped.\nbody=' + body)


def test_landing_actions_column_is_wide_enough():
    """The landing action column must stay >= 640px so the arxiv input keeps
    room for its full placeholder and the card doesn't strand in side-gutters."""
    body = _rule_body(_read(CSS), '.paper-landing-actions')
    assert body is not None, (
        '.paper-landing-actions rule not found (structure changed?)')
    mw = _max_width_px(body)
    assert mw is not None, (
        '.paper-landing-actions has no `max-width:<N>px` — it must keep an '
        f'explicit cap >= {MIN_WIDTH_PX}px.\nbody=' + body)
    assert mw >= MIN_WIDTH_PX, (
        f'.paper-landing-actions max-width shrank to {mw}px (< {MIN_WIDTH_PX}px) '
        '— this re-clips the arxiv placeholder and re-opens the dead side-gutters '
        'the 2026-07-08 / 2026-09-10 fixes closed.\nbody=' + body)


def test_nc_narrow_column_is_flagged():
    """NEUTER: revert the cap to the old 400px on a COPY of the body → the
    >= 520px assertion must be able to catch it (proves the guard is
    load-bearing, not vacuously passing)."""
    body = _rule_body(_read(CSS), '.paper-landing-actions')
    assert body is not None and _max_width_px(body) >= MIN_WIDTH_PX, 'fix real CSS first'
    poisoned = re.sub(r'max-width\s*:\s*\d+px', 'max-width:400px', body)
    mw = _max_width_px(poisoned)
    assert mw == 400, 'neuter did not rewrite the cap — test is stale'
    assert not (mw >= MIN_WIDTH_PX), (
        'the >= 520px assertion would NOT flag a 400px regression — guard is not '
        'load-bearing.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
