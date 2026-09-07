"""Guard: bottom-sheet CONTENT reaches every touch/narrow viewport that can
open the "···" sheet — and does NOT leak onto a fine-pointer desktop.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -m unit \
        tests/test_mobile_sheet_content_reaches_wide_coarse.py

Root-cause guard for the "wide coarse tablet opens an UNSTYLED ··· sheet" bug.
static/styles.css historically had THREE breakpoint blocks that each hand-copied
the `.mobile-bottom-sheet` CONTAINER, but the `.mobile-sheet-*` / `.mobile-depth-*`
CONTENT rules were copied into only two of them (≤768 and 769–1024 coarse). The
≥1025 coarse block mirrored the container but not the content, so a 1280px
Android-WebView tablet popped the sheet with bare-text rows and naked depth
buttons. The fix consolidates container + content + tofu variants into ONE
`@media (max-width:768px),(pointer:coarse)` block placed after all width blocks.

This test parses styles.css, evaluates which top-level `@media` blocks a
synthetic viewport matches (a plain media-query intersection over the source —
no browser), and asserts:
  • {width:1300, pointer:coarse}  → sheet CONTENT selectors ARE reachable.
  • {width:1300, pointer:fine}    → they are NOT (a narrowed desktop window
    keeps the full toolbar and never opens the sheet).
"""
from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

_STYLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'styles.css',
)

# Selectors that make the sheet CONTENT visible. All must be reachable on a
# coarse viewport and unreachable on a fine one.
_REQUIRED = [
    '.mobile-sheet-header',
    '.mobile-sheet-section',
    '.mobile-sheet-section-title',
    '.mobile-sheet-item',
    '.mobile-sheet-item-icon',
    '.mobile-sheet-item-text',
    '.mobile-sheet-item-name',
    '.mobile-sheet-item-desc',
    '.mobile-sheet-item-check',
    '.mobile-sheet-item.disabled',
    '.mobile-depth-bar',
    '.mobile-depth-btn',
    '.mobile-depth-btn.active',
    '[data-theme="tofu"] .mobile-bottom-sheet',
]


def _load_css() -> str:
    with open(_STYLES, 'r', encoding='utf-8') as f:
        css = f.read()
    # Strip comments FIRST — a comment near the tablet blocks literally contains
    # the text "@media(...)", which would otherwise be parsed as a real block.
    return re.sub(r'/\*.*?\*/', '', css, flags=re.S)


def _px(val: str) -> int:
    return int(re.sub(r'[^0-9]', '', val))


def _match_cond(cond: str, vp: dict) -> bool:
    cond = cond.strip().strip('()').strip()
    if not cond:
        return True
    feat, _sep, val = cond.partition(':')
    feat, val = feat.strip(), val.strip()
    if feat == 'pointer':
        return vp.get('pointer') == val
    if feat == 'max-width':
        return vp['width'] <= _px(val)
    if feat == 'min-width':
        return vp['width'] >= _px(val)
    # Unknown feature (screen / prefers-* / orientation …) → does not match our vp.
    return False


def _match_query(query: str, vp: dict) -> bool:
    # Comma = OR of query lists; each list is AND-joined conditions.
    for part in query.split(','):
        conds = [c for c in re.split(r'\band\b', part) if c.strip()]
        if conds and all(_match_cond(c, vp) for c in conds):
            return True
    return False


def _iter_media_blocks(css: str):
    """Yield (query, body) for each top-level @media block, brace-balanced."""
    i, n = 0, len(css)
    while True:
        m = css.find('@media', i)
        if m == -1:
            return
        brace = css.find('{', m)
        if brace == -1:
            return
        query = css[m + 6:brace].strip()
        depth, j = 1, brace + 1
        while j < n and depth:
            ch = css[j]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            j += 1
        yield query, css[brace + 1:j - 1]
        i = j


def _matched_body(css: str, vp: dict) -> str:
    return '\n'.join(body for q, body in _iter_media_blocks(css)
                     if _match_query(q, vp))


def _has_selector(body: str, sel: str) -> bool:
    # Selector followed by a rule/combinator boundary ({ , : . or whitespace).
    return re.search(re.escape(sel) + r'\s*[{,:.\s]', body) is not None


def test_content_reaches_wide_coarse_tablet():
    css = _load_css()
    body = _matched_body(css, {'width': 1300, 'pointer': 'coarse'})
    missing = [s for s in _REQUIRED if not _has_selector(body, s)]
    assert not missing, (
        'Sheet CONTENT selectors NOT reachable at {width:1300,pointer:coarse} '
        '(wide coarse tablet renders an unstyled ··· sheet): %s' % missing
    )


def test_content_absent_on_wide_fine_desktop():
    css = _load_css()
    body = _matched_body(css, {'width': 1300, 'pointer': 'fine'})
    leaked = [s for s in _REQUIRED if _has_selector(body, s)]
    assert not leaked, (
        'Sheet layout leaked onto a fine-pointer desktop narrow window '
        '(should keep the full toolbar, never open the sheet): %s' % leaked
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
