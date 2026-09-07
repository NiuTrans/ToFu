"""Mobile "···" more-sheet coordination drift-guard.

ROOT CAUSE this guards (2026-07-11): the mobile bottom sheet was orphaned by a
media-query breakpoint mismatch. The "···" more-button (`.mobile-more-btn`) —
the ONLY affordance that opens `#mobileSheet`, and the sheet houses the tool
toggles — is revealed (`display:flex`) in one @media block per viewport shape
(phone ≤768, tablet 769–1024 coarse, wide-coarse ≥1025), but the rules that
make the sheet actually VISIBLE when JS adds `.open`
(`.mobile-bottom-sheet{position:fixed…}` + `.mobile-bottom-sheet.open{display:block}`)
originally lived ONLY in the ≤768 phone block — so a coarse-pointer tablet
showed the button yet opened a `display:none` sheet.

THE FIX ARCHITECTURE (current): instead of mirroring the sheet rules into
EVERY reveal block, the sheet container+content rules live in ONE union block

    @media (max-width:768px),(pointer:coarse){ …sheet rules… }

placed AFTER all width blocks, whose media query is a superset of every
viewport that can reveal the more-button (≤768 any pointer ∪ any coarse
pointer). Fine-pointer desktops never reveal the button and never get the
sheet layout.

INVARIANT (re-anchored): every @media block that reveals `.mobile-more-btn`
must have its viewport COVERED by the union sheet block — i.e. its header must
be either ≤768 (`max-width:768px`, any pointer) or coarse-pointer
(`pointer:coarse`) — AND the union sheet block must exist and define both
`.mobile-bottom-sheet{position:fixed}` and
`.mobile-bottom-sheet.open{display:block}`. A reveal block outside the union
(e.g. a fine-pointer-only tablet block) re-creates the 2026-07-11 orphan.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
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
    r'\.mobile-more-btn\s*\{[^}]*\bdisplay\s*:\s*(?:flex|block)\b')
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
    """Core check, shared by the guard test and the NC: return the reveal-block
    headers NOT covered by any union sheet block (empty list = invariant holds)."""
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
    """CORE INVARIANT: the union sheet block must exist (both arms) and every
    @media block that shows the '···' affordance must be viewport-covered by it
    (≤768 any-pointer, or pointer:coarse). A reveal block outside the union
    re-creates the 2026-07-11 orphan (button opens a display:none sheet →
    tools unreachable)."""
    css = _read(CSS)
    offenders = _find_offenders(css)
    assert not offenders, (
        "mobile '···' more-button is revealed in an @media block NOT covered "
        "by the union sheet block `@media (max-width:768px),(pointer:coarse)` "
        "— the button will open a display:none sheet (the 2026-07-11 "
        "breakpoint-orphan bug). Offending reveal blocks:\n"
        + '\n'.join(f'  @media {h}' for h in offenders))


# ─────────────────────── NEUTER (load-bearing proof) ───────────────────────

def test_neuter_orphaned_sheet_is_flagged():
    """Prove the guard bites: strip the open-sheet rules out of the union cover
    block — the pre-fix arrangement — and confirm the core assertion would
    FAIL. Mutates an in-memory copy only; the file on disk is untouched."""
    css = _COMMENT_RE.sub('', _read(CSS))

    def _neuter(text: str) -> str:
        result = text
        for header, body in _iter_media_blocks(text):
            if _SHEET_OPEN_RE.search(body) and _SHEET_FIXED_RE.search(body):
                neutered_body = _SHEET_OPEN_RE.sub('', body)
                neutered_body = re.sub(
                    r'\.mobile-bottom-sheet\s*\{[^}]*position\s*:\s*fixed[^}]*\}',
                    '', neutered_body)
                result = result.replace(body, neutered_body, 1)
        return result

    neutered = _neuter(css)
    assert neutered != css, (
        'neuter did not change the CSS — the union sheet-cover block was not '
        'found; the fix location moved, update this NC.')

    assert _find_offenders(neutered), (
        'neutered CSS (open-sheet rules stripped from the union block) was NOT '
        'flagged — the guard is a tautology and would not catch a real orphan.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
