"""Every visible composer More entry must open a styled, viewport-fixed sheet.

The shared sheet now serves both touch toolbars and desktop paper splits.
Keep the fault injection: removing its visibility rules must orphan the entry.
Real geometry and keyboard behavior are covered by the journaled TeX journey.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

_COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _iter_media_blocks(css: str):
    """Yield (header, body) for every top-level `@media …{ … }` block,
    brace-matched so nested `{}` (e.g. rule bodies) don't end the block early.
    CSS comments are stripped first so a `@media …` mentioned in a comment is
    never mistaken for a real block header."""
    css = _COMMENT_RE.sub('', css)
    for m in re.finditer(r'@media[^{]*\{', css):
        header = m.group(0)[:-1].strip()
        brace = m.end() - 1
        depth = 0
        i = brace
        while i < len(css):
            if css[i] == '{':
                depth += 1
            elif css[i] == '}':
                depth -= 1
                if depth == 0:
                    yield header, css[brace + 1:i]
                    break
            i += 1


# A block "reveals" the more-button when it sets it to a visible display value
# (display:flex / display:block, with or without !important).
_MORE_BTN_SHOW_RE = re.compile(
    r'\.mobile-more-btn\s*\{[^}]*\bdisplay\s*:\s*(?:inline-flex|flex|block)\b')
# The two rules that make the sheet actually appear when JS adds `.open`.
_SHEET_OPEN_RE = re.compile(
    r'\.mobile-bottom-sheet\.open\s*\{[^}]*\bdisplay\s*:\s*block\b')
_SHEET_FIXED_RE = re.compile(
    r'\.mobile-bottom-sheet\s*\{[^}]*\bposition\s*:\s*fixed\b')


def _blocks_revealing_more_btn(css: str):
    return [(h, b) for h, b in _iter_media_blocks(css) if _MORE_BTN_SHOW_RE.search(b)]


def _sheet_cover_blocks(css: str):
    """@media blocks that define BOTH sheet-visibility rules (the union cover)."""
    return [(h, b) for h, b in _iter_media_blocks(css)
            if _SHEET_OPEN_RE.search(b) and _SHEET_FIXED_RE.search(b)]


def _covered_by_union(header: str) -> bool:
    """A reveal block's viewport is covered by the union sheet block
    `@media (max-width:768px),(pointer:coarse)` iff it is either the ≤768
    any-pointer arm or ANY coarse-pointer arm (769–1024, ≥1025, unbounded)."""
    return 'pointer:coarse' in header or 'max-width:768px' in header


def _find_offenders(css: str):
    """Accept unconditional sheet rules or media rules covering each entry."""
    unconditional = _COMMENT_RE.sub('', css)
    for _, body in _iter_media_blocks(css):
        unconditional = unconditional.replace(body, '', 1)
    if _SHEET_OPEN_RE.search(unconditional) and _SHEET_FIXED_RE.search(unconditional):
        return []
    covers = _sheet_cover_blocks(css)
    # The cover must include the union arms (≤768 any-pointer + coarse). A cover
    # block keyed on something narrower (e.g. only ≤768) leaves coarse tablets
    # orphaned, so require BOTH arms across the cover set.
    union_ok = any('pointer:coarse' in h for h, _ in covers) and \
        any('max-width:768px' in h for h, _ in covers)
    if not union_ok:
        return [h for h, _ in _blocks_revealing_more_btn(css)] or ['<no cover block>']
    return [h for h, _ in _blocks_revealing_more_btn(css)
            if not _covered_by_union(h)]


def test_more_button_blocks_exist():
    """Sanity: at least one @media block reveals the '···' more-button, else the
    coordination assertion below would pass vacuously."""
    css = _read(CSS)
    blocks = _blocks_revealing_more_btn(css)
    assert blocks, (
        "no @media block sets .mobile-more-btn to display:flex/block — the "
        "more-button reveal moved; update this guard.")


def test_every_more_button_block_also_defines_open_sheet():
    """A revealed entry cannot depend on a narrower sheet breakpoint."""
    offenders = _find_offenders(_read(CSS))
    assert not offenders, 'More entry has no reachable fixed sheet: ' + repr(offenders)


# ─────────────────────── NEUTER (load-bearing proof) ───────────────────────

def test_neuter_orphaned_sheet_is_flagged():
    """Remove visibility rules in memory; the orphaned entry must be caught."""
    css = _COMMENT_RE.sub('', _read(CSS))
    neutered = re.sub(r'\.mobile-bottom-sheet\.open\s*\{[^}]*\}', '', css)
    assert neutered != css, 'fault injection did not remove the sheet open rule'
    assert _find_offenders(neutered), 'missing sheet visibility was not caught'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
