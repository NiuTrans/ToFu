"""Mobile "···" more-sheet coordination drift-guard.

ROOT CAUSE this guards (2026-07-11): the mobile bottom sheet was orphaned by a
media-query breakpoint mismatch. The "···" more-button (`.mobile-more-btn`) —
the ONLY affordance that opens `#mobileSheet`, and the sheet houses the tool
toggles (code exec / memory / browser / image-gen / swarm / …) — was revealed
(`display:flex`) in TWO media blocks:

  * `@media(min-width:769px) and (max-width:1024px) and (pointer:coarse)`  (tablet)
  * `@media(max-width:768px)`                                             (phone)

but the rules that make the sheet actually VISIBLE when JS adds `.open`
(`.mobile-bottom-sheet{position:fixed…}` + `.mobile-bottom-sheet.open{display:block}`)
lived ONLY in the ≤768 phone block. On a 769–1024px coarse-pointer viewport
(tablets, and the Android WebView when it reports a CSS width >768) the button
appeared and JS added `.open`, but the base top-level
`.mobile-bottom-sheet{display:none}` was never overridden → the sheet opened at
`display:none` → tools unreachable in BOTH Chrome (tablet width) and the
WebView. The JS open-path (`toggleMobileSheet` → `sheet.classList.add("open")`,
bound via inline `onclick` on the button) has NO viewport guard, so the CSS
orphan was the sole defect.

INVARIANT: every `@media` block that reveals `.mobile-more-btn` (i.e. exposes
the "···" affordance) MUST also define, in the SAME block, both
`.mobile-bottom-sheet.open{display:block}` and `.mobile-bottom-sheet{position:fixed}`
— otherwise the button opens a sheet the cascade keeps hidden. This is the same
"rule present in one @media block but orphaned in the sibling block" bug class
that tests/test_breakpoint_coordination.py and tests/test_tablet_label_truncation.py
already guard.
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


def _iter_media_blocks(css: str):
    """Yield (header, body) for every top-level `@media …{ … }` block,
    brace-matched so nested `{}` (e.g. rule bodies) don't end the block early."""
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


def test_more_button_blocks_exist():
    """Sanity: at least one @media block reveals the '···' more-button, else the
    coordination assertion below would pass vacuously."""
    css = _read(CSS)
    blocks = _blocks_revealing_more_btn(css)
    assert blocks, (
        "no @media block sets .mobile-more-btn to display:flex/block — the "
        "more-button reveal moved; update this guard.")


def test_every_more_button_block_also_defines_open_sheet():
    """CORE INVARIANT: any @media block that shows the '···' affordance must, in
    the SAME block, also define `.mobile-bottom-sheet.open{display:block}` and
    `.mobile-bottom-sheet{position:fixed}`. A block that reveals the button but
    lacks the open-sheet rules re-creates the 2026-07-11 orphan (button opens a
    display:none sheet → tools unreachable)."""
    css = _read(CSS)
    offenders = []
    for header, body in _blocks_revealing_more_btn(css):
        missing = []
        if not _SHEET_OPEN_RE.search(body):
            missing.append('.mobile-bottom-sheet.open{display:block}')
        if not _SHEET_FIXED_RE.search(body):
            missing.append('.mobile-bottom-sheet{position:fixed}')
        if missing:
            offenders.append(f'  @media {header}\n      missing: {", ".join(missing)}')
    assert not offenders, (
        "mobile '···' more-button is revealed in an @media block that does NOT "
        "define the bottom-sheet visibility rules — the button will open a "
        "display:none sheet (the 2026-07-11 breakpoint-orphan bug). Mirror "
        "`.mobile-bottom-sheet{position:fixed}` + `.mobile-bottom-sheet.open"
        "{display:block}` into each such block:\n" + '\n'.join(offenders))


# ─────────────────────── NEUTER (load-bearing proof) ───────────────────────

def test_neuter_orphaned_sheet_is_flagged():
    """Prove the guard bites: strip the open-sheet rules out of the tablet
    (769–1024 coarse) block — the exact pre-fix arrangement — and confirm the
    core assertion would FAIL. Mutates an in-memory copy only; the file on disk
    is untouched."""
    css = _read(CSS)

    # Locate the tablet coarse-pointer block that reveals the more-button and
    # delete its `.mobile-bottom-sheet{position:fixed…}` + `.open{display:block}`
    # rules, reproducing the orphan.
    def _neuter(text: str) -> str:
        # Walk media blocks; for the target tablet block, drop the sheet rules.
        result = text
        for header, body in _iter_media_blocks(text):
            if ('pointer:coarse' in header and 'min-width:769px' in header
                    and _MORE_BTN_SHOW_RE.search(body)):
                neutered_body = _SHEET_OPEN_RE.sub('', body)
                neutered_body = re.sub(
                    r'\.mobile-bottom-sheet\s*\{[^}]*position\s*:\s*fixed[^}]*\}',
                    '', neutered_body)
                result = result.replace(body, neutered_body, 1)
        return result

    neutered = _neuter(css)
    assert neutered != css, (
        'neuter did not change the CSS — the tablet coarse-pointer more-button '
        'block was not found; the fix location moved, update this NC.')

    # Re-run the core check logic against the neutered copy: it MUST find an offender.
    offenders = []
    for header, body in _iter_media_blocks(neutered):
        if not _MORE_BTN_SHOW_RE.search(body):
            continue
        if not (_SHEET_OPEN_RE.search(body) and _SHEET_FIXED_RE.search(body)):
            offenders.append(header)
    assert offenders, (
        'neutered CSS (open-sheet rules stripped from the tablet block) was NOT '
        'flagged — the guard is a tautology and would not catch a real orphan.')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
