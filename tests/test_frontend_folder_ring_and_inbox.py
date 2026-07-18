"""Project-rail selection-ring + default-folder inbox-tile drift guard.

Two symptoms this guards against (fixed 2026-07-08):

1. RING VANISHES WHILE GENERATING. In the collapsed (icon-only) rail the
   "selected folder" indicator is a ring on `.folder-tab.active .folder-tab-dot`.
   It was drawn with `box-shadow`. But a folder whose conversation is generating
   gets `.streaming`, and the tofu-theme streaming animation (`tofuBreathing`)
   ANIMATES `box-shadow` — an active animation overrides the static rule, so the
   selection ring was entirely replaced by the pulse glow and DISAPPEARED
   (reported: "selecting a folder that has a generating conversation shows no
   circle"). Fix: draw the ring with `outline` (no keyframe touches outline), so
   it survives streaming on every theme.

   INVARIANT: the two `rail-collapsed .folder-tab.active .folder-tab-dot` ring
   rules must use `outline`, NOT `box-shadow` (which any *Breathing keyframe can
   clobber).

2. DEFAULT (未分类) TILE CLASHED + is now enlarged & blinks. Its inner glyph was
   only 12px in a 20px tile while project tiles are bold colored monograms. Fix:
   the inbox svg is enlarged and given a periodic `folderInboxBlink` animation.

   INVARIANT: `.folder-tab-inbox-dot svg` carries the `folderInboxBlink`
   animation AND the keyframe is defined.

Env-independent: parses static/styles.css directly. NEUTERs included.
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


def _ring_decls(css: str) -> list[str]:
    """Declaration bodies of every `rail-collapsed .folder-tab.active
    .folder-tab-dot{…}` rule (there are two: desktop base + drawer block)."""
    compact = re.sub(r'\s+', '', css)
    pat = r'rail-collapsed\.folder-tab\.active\.folder-tab-dot\{([^{}]*)\}'
    return re.findall(pat, compact)


def _ring_uses_boxshadow(css: str) -> list[str]:
    return [d for d in _ring_decls(css) if 'box-shadow' in d]


def test_collapsed_ring_uses_outline_not_boxshadow():
    """The collapsed-rail selection ring must not be a box-shadow — a streaming
    folder's box-shadow-animating keyframe would clobber it, hiding the ring."""
    css = _read(CSS)
    decls = _ring_decls(css)
    assert decls, 'collapsed-rail active ring rule not found (structure changed?)'
    offenders = _ring_uses_boxshadow(css)
    assert not offenders, (
        'the collapsed-rail selection ring is drawn with box-shadow — a '
        'streaming folder animates box-shadow and clobbers it, so the ring '
        'vanishes. Use `outline`:\n' + '\n'.join(offenders))
    for d in decls:
        assert 'outline' in d, f'ring rule missing outline: {d}'


def test_inbox_glyph_blinks_and_keyframe_defined():
    """The default (未分类) tile glyph must carry the folderInboxBlink animation
    and the keyframe must exist."""
    css = _read(CSS)
    compact = re.sub(r'\s+', '', css)
    m = re.search(r'\.folder-tab-inbox-dotsvg\{([^{}]*)\}', compact)
    assert m, '.folder-tab-inbox-dot svg rule not found'
    assert 'folderInboxBlink' in m.group(1), (
        'the inbox glyph lost its folderInboxBlink animation')
    assert '@keyframesfolderInboxBlink{' in compact, (
        'folderInboxBlink keyframe is not defined')


def test_inbox_glyph_is_enlarged():
    """The base inbox glyph must be larger than the 12px system-chip default."""
    css = _read(CSS)
    compact = re.sub(r'\s+', '', css)
    m = re.search(r'\.folder-tab-inbox-dotsvg\{([^{}]*)\}', compact)
    assert m, '.folder-tab-inbox-dot svg rule not found'
    w = re.search(r'width:(\d+)px', m.group(1))
    assert w and int(w.group(1)) > 12, (
        f'inbox glyph not enlarged past 12px (got {m.group(1)})')


# ── NEUTERs: prove each guard is load-bearing ──

def test_nc_boxshadow_ring_is_flagged():
    css = _read(CSS)
    assert not _ring_uses_boxshadow(css), 'real CSS not clean; fix before NC'
    poisoned = css.replace(
        '.sidebar.rail-collapsed .folder-tab.active .folder-tab-dot{outline:2px solid var(--accent);outline-offset:2px}',
        '.sidebar.rail-collapsed .folder-tab.active .folder-tab-dot{box-shadow:0 0 0 4px var(--accent)}',
        1)
    assert poisoned != css, 'NC anchor not found — desktop ring rule text drifted'
    assert _ring_uses_boxshadow(poisoned), (
        'the guard did NOT catch a box-shadow-drawn ring — not detecting the '
        'regression class.')


def test_nc_missing_blink_is_flagged():
    css = _read(CSS)
    poisoned = css.replace(
        '.folder-tab-inbox-dot svg{width:15px;height:15px;animation:folderInboxBlink 4.8s ease-in-out infinite}',
        '.folder-tab-inbox-dot svg{width:12px;height:12px}',
        1)
    assert poisoned != css, 'NC anchor not found — inbox svg rule text drifted'
    compact = re.sub(r'\s+', '', poisoned)
    m = re.search(r'\.folder-tab-inbox-dotsvg\{([^{}]*)\}', compact)
    # After neuter the first-matched rule loses the blink → guard would fail.
    assert m and 'folderInboxBlink' not in m.group(1), (
        'neuter did not remove the blink from the primary inbox svg rule')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
