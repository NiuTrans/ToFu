"""Drift-guard for the paper-reader landing action-column width (2026-07-08).

Root cause it locks in: the landing / upload card column
``.paper-landing-actions`` was hard-capped at ``max-width:400px`` regardless of
the PDF pane width. In a wide desktop pane that stranded the card between large
side-gutters AND squeezed the arxiv search ``<input>`` (which shares its row
with the 搜索 button and reserves ~36px of left padding for the search icon)
too narrow to render its full placeholder ``搜索标题，或粘贴 arXiv 链接 / 编号``.
The fix raised the cap to 520px.

A bare ``max-width`` value is exactly the kind of thing a later responsive edit
can silently shrink back — re-clipping the placeholder with NO error. This guard
pins ``.paper-landing-actions`` max-width at >= 520px so that regression fails
loudly.

Env-independent: parses static/styles.css directly (no node/jsdom). A NEUTER
reverts the cap to 400px and confirms the assertion would fire.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

MIN_WIDTH_PX = 520


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _strip_comments(css: str) -> str:
    """Remove /* … */ comments (they can contain literal braces that corrupt the
    naive brace-based rule splitter — the documented styles.css test trap)."""
    return re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)


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


def test_landing_actions_column_is_wide_enough():
    """The landing action column must stay >= 520px so the arxiv input keeps
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
        'the 2026-07-08 fix closed.\nbody=' + body)


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
