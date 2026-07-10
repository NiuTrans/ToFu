"""Portrait-tablet label-truncation drift-guard.

Symptom this guards against (fixed 2026-07-07, the THIRD hand-fix of this
class): a tablet in PORTRAIT runs the PC-mode layout at 769–1024px wide with a
COARSE pointer. Several `@media(max-width:Npx)` compaction breakpoints were
authored to shrink genuinely cramped NARROW DESKTOP windows — but a *width-only*
media query cannot tell a narrow desktop window from a roomy portrait tablet, so
it ALSO fires on the tablet and hides text labels there, truncating them to bare
icons / number badges despite ample room (topbar Paper/My Day/Studio/Tasks, the
Search toggle label, …).

Root-cause fix: those width-only compaction blocks are gated on
`and (pointer:fine)` so they only ever compact a real (fine-pointer) desktop
window; a coarse-pointer tablet keeps its labels. Coarse PHONES (≤768) get their
own icon-only treatment inside the phone `@media(max-width:768px)` block.

INVARIANT (this test): no `@media` block that (a) has a `max-width` landing in
the portrait-tablet band [769, 1024] and (b) is NOT constrained to
`pointer:fine` may set a known TEXT-LABEL selector to `display:none`. Such a
rule would fire on a coarse tablet and truncate the label. A block that IS
`pointer:fine` (desktop-only) is exempt; a `pointer:coarse`/no-upper-bound band
is fine too (that's the tablet block itself, which SHOWS labels).

The `≤768` phone block is out of band (768 < 769) and may freely hide labels.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
CSS = os.path.join(ROOT, 'static', 'styles.css')

# Text-label selectors: their ENTIRE visible meaning is the text (icon-less or
# label-dominant). Hiding them on a roomy tablet is the truncation bug.
_LABEL_SELECTORS = (
    '.topbar-tool-label',
    '.update-btn-label',
    '.sm-label',
    '.submenu-label',
)

# The portrait-tablet band (inclusive). 768 and below is the phone block.
_BAND_LO = 769
_BAND_HI = 1024


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


def _iter_media_blocks(css: str):
    """Yield (header, body) for every top-level @media block, brace-matched.

    header = the raw text between '@media' and the opening '{' (the condition).
    body   = the block contents (may itself contain nested braces for rules).
    """
    for m in re.finditer(r'@media([^{]*)\{', css):
        header = m.group(1)
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


def _block_hides_label(body: str) -> list[str]:
    """Return the label selectors this block sets to display:none.

    We match a selector list containing the label immediately followed by a
    declaration block that contains `display:none`. Whitespace-insensitive.
    """
    hits = []
    compact = re.sub(r'\s+', '', body)
    for sel in _LABEL_SELECTORS:
        # <sel> ... { ... display:none ... }  — the selector's own rule block.
        # Find each occurrence of the selector, then look at the {...} that
        # governs it (the next '{' ... matching '}').
        for mm in re.finditer(re.escape(sel), compact):
            # Walk forward to the governing '{'; if we hit a '}' or ',' first
            # the selector is part of a list — keep scanning to the block '{'.
            j = mm.end()
            # Selectors may be comma-joined; scan to the next '{'.
            brace = compact.find('{', j)
            if brace == -1:
                continue
            # Ensure no intervening '}' (which would mean this selector had its
            # own separate empty context) — simple heuristic: the segment from
            # selector to '{' must not contain '}'.
            if '}' in compact[j:brace]:
                continue
            end = compact.find('}', brace)
            if end == -1:
                continue
            decls = compact[brace + 1:end]
            if 'display:none' in decls:
                hits.append(sel)
                break
    return hits


def _band_max_widths(header: str) -> list[int]:
    """max-width values in this media header that land in the tablet band."""
    return [int(n) for n in re.findall(r'max-width:\s*(\d+)px', header)
            if _BAND_LO <= int(n) <= _BAND_HI]


def _is_fine_only(header: str) -> bool:
    """True if the media condition constrains to pointer:fine (desktop only)."""
    return bool(re.search(r'pointer:\s*fine', header))


def _offending_blocks(css: str) -> list[str]:
    """The core scan: width-in-band, NOT fine-only, hides a label."""
    offenders = []
    for header, body in _iter_media_blocks(css):
        if not _band_max_widths(header):
            continue
        if _is_fine_only(header):
            continue  # desktop-only compaction — never reaches a coarse tablet
        hidden = _block_hides_label(body)
        if hidden:
            offenders.append(
                f'@media{header.strip()} hides {sorted(set(hidden))}')
    return offenders


# ─────────────────────────── the invariant ───────────────────────────

def test_no_label_hidden_in_uncoarse_tablet_band():
    """No width-only (or coarse) @media covering 769–1024px may hide a text
    label — that truncates it on a portrait tablet. Desktop-only
    (`pointer:fine`) compaction is exempt."""
    css = _read(CSS)
    offenders = _offending_blocks(css)
    assert not offenders, (
        'A text label is hidden by a media block that fires on a PORTRAIT '
        'TABLET (769–1024px, coarse pointer). Gate the block on '
        '`and (pointer:fine)` so it only compacts a narrow desktop window:\n'
        + '\n'.join(offenders))


def test_the_two_known_blocks_are_fine_gated():
    """Positive lock on the two historically-offending blocks: the ≤1080 topbar
    label hide and the ≤1024 toolbar compaction MUST carry pointer:fine. Uses
    the brace-matched iterator (the ≤1024 block nests `.depth-btn{…}` before
    `.sm-label`, so a flat `[^}]*` regex can't reach it)."""
    css = _read(CSS)
    found_1080 = found_1024 = False
    for header, body in _iter_media_blocks(css):
        compact = re.sub(r'\s+', '', body)
        widths = re.findall(r'max-width:\s*(\d+)px', header)
        if '1080' in widths and '.topbar-tool-label' in compact and 'display:none' in compact:
            found_1080 = True
            assert _is_fine_only(header), (
                'the ≤1080 topbar-label hide lost its `and (pointer:fine)` guard '
                '— it will truncate topbar tool labels on portrait tablets '
                f'again. header=@media{header.strip()}')
        if '1024' in widths and '.sm-label' in compact and 'display:none' in compact:
            found_1024 = True
            assert _is_fine_only(header), (
                'the ≤1024 sm-label hide lost its `and (pointer:fine)` guard. '
                f'header=@media{header.strip()}')
    assert found_1080, '≤1080 topbar-label hide block not found (structure changed?)'
    assert found_1024, '≤1024 sm-label hide block not found (structure changed?)'


# ─────────────────────────── NEUTER control ───────────────────────────

def test_nc_scanner_catches_an_injected_regression():
    """POISONED-FIXTURE NC: inject a width-only ≤1000px block that hides a
    label into the real CSS and confirm the scanner FLAGS it. Proves the scan
    is load-bearing — not a tautology that passes because nothing matches."""
    css = _read(CSS)
    # Precondition: the real file is currently clean.
    assert not _offending_blocks(css), 'real CSS is not clean; fix before NC'
    poisoned = css + (
        '\n@media(max-width:1000px){.topbar-tool-label{display:none}}\n')
    offenders = _offending_blocks(poisoned)
    assert offenders, (
        'the scanner did NOT catch an injected width-only ≤1000px label hide — '
        'it is not actually detecting the regression class.')
    assert any('1000' not in o for o in offenders) or offenders, offenders


def test_nc_fine_gated_injection_is_not_flagged():
    """Complementary NC: the SAME injected block, but gated on pointer:fine,
    must NOT be flagged (proves the fine-pointer exemption is honored and the
    guard isn't over-broadly failing every ≤1024 block)."""
    css = _read(CSS)
    ok = css + (
        '\n@media(max-width:1000px) and (pointer:fine){.topbar-tool-label{display:none}}\n')
    offenders = _offending_blocks(ok)
    assert not offenders, (
        'a pointer:fine-gated desktop compaction was wrongly flagged — the '
        'exemption for narrow desktop windows is broken:\n' + '\n'.join(offenders))


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
