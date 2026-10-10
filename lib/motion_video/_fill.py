"""lib/motion_video/_fill.py — vertical-fill gate (real-browser measurement).

Why this is a separate gate rather than part of ``hyperframes check``: the CLI
is an external npm binary (``hyperframes v0.7.71``) and its ``inspect``
subcommand reports only text/container OVERFLOW — content spilling OUT of the
frame. It has no opinion on the opposite defect, content occupying a fraction
of a tall frame, and exposes no geometry we could derive one from. So the
measurement needs its own headless-Chrome boot; measured cost is ~1.2s per
scene, against a render that costs ~3.5x realtime.

Why the gate exists at all (measured, not hypothesized): before the craft
guide gained vertical-composition instructions, authored scenes on this host
had a mean vertical span of 65.0% with a mean bottom dead-band of 19.5%, worst
two at 34.0% and 33.3%. A 1080x1440 frame with a third of its height empty is
exactly what a viewer describes as "unformatted" -- and every static gate
passed it, because a frame can be perfectly well-formed and still be mostly
nothing.

Guidance alone cannot hold that: a paragraph in a prompt is not a gate, and
the next model that ignores it regresses every frame with no test turning red.
"""

from __future__ import annotations

import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['check_composition_fill', 'measure_fill', 'findings_for_fill',
           'MIN_VERTICAL_SPAN', 'MAX_BOTTOM_DEAD_BAND']

#: Minimum share of frame HEIGHT that visible content must span.
#:
#: Calibrated against the real 9-scene authored corpus on this host, whose
#: spans were 49.6 / 58.5 / 59.2 | 68.2 / 69.5 / 72.2 / 78.0 / 86.1 / 87.8 %.
#: The gap between 59.2 and 68.2 separates the three scenes that prompted this
#: work from every scene a viewer had no complaint about, so the threshold
#: sits in that gap rather than on a round number that looked nice.
MIN_VERTICAL_SPAN = 0.60

#: Maximum share of frame height allowed to be EMPTY below the last content.
#:
#: Same corpus, bottom dead-bands 2.9 / 6.1 / 6.9 / 6.9 / 13.9 / 20.3 | 25.2 /
#: 33.3 / 34.0 %. The threshold sits above 20.3 (a scene nobody objected to)
#: and below 25.2, again in the measured gap. A span check alone would miss a
#: composition that is tall but sits high in the frame.
MAX_BOTTOM_DEAD_BAND = 0.28

#: Measures the visible ink box of a composition, in frame-relative terms.
#:
#: Walks the element tree collecting nodes that actually paint (leaf text,
#: images, SVG, a non-transparent background) and unions their client rects.
#: Full-bleed backdrops are excluded -- a 100%x100% gradient would otherwise
#: report a perfect 100% span for a frame containing one centred line, which
#: is precisely the composition this gate exists to reject.
_MEASURE_JS = r'''
() => {
  const root = document.querySelector('[data-composition-id]') || document.body;
  const H = parseFloat(root.getAttribute('data-height')) || window.innerHeight;
  const W = parseFloat(root.getAttribute('data-width')) || window.innerWidth;
  let top = Infinity, bottom = -Infinity, n = 0;
  const walk = (el) => {
    for (const c of el.children) {
      const cs = getComputedStyle(c);
      if (cs.display === 'none' || cs.visibility === 'hidden'
          || parseFloat(cs.opacity) === 0) continue;
      const r = c.getBoundingClientRect();
      const isBackdrop = (r.width >= W * 0.98 && r.height >= H * 0.98);
      const paints = (c.childElementCount === 0 && c.textContent.trim())
                     || cs.backgroundImage !== 'none'
                     || (cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && !isBackdrop)
                     || c.tagName === 'IMG'
                     || String(c.tagName).toLowerCase() === 'svg';
      if (paints && !isBackdrop && r.height > 0 && r.width > 0) {
        top = Math.min(top, r.top); bottom = Math.max(bottom, r.bottom); n++;
      }
      walk(c);
    }
  };
  walk(root);
  if (!isFinite(top)) return null;
  return {H: H, W: W, top: top, bottom: bottom, n: n};
}
'''

_WIDTH_RE = re.compile(r'data-width="(\d+)"')
_HEIGHT_RE = re.compile(r'data-height="(\d+)"')


def _frame_size(html: str) -> tuple[int, int]:
    mw, mh = _WIDTH_RE.search(html or ''), _HEIGHT_RE.search(html or '')
    return (int(mw.group(1)) if mw else 1080,
            int(mh.group(1)) if mh else 1440)


def measure_fill(html: str, *, timeout_ms: int = 20000) -> dict | None:
    """Measure one composition's vertical fill in a real headless browser.

    Returns ``{'span', 'bottom_dead', 'top_dead', 'nodes'}`` as frame-relative
    fractions, or ``None`` when the measurement could not be taken (no
    playwright, no Chromium, page error). ``None`` means UNKNOWN, never
    "clean" -- callers must not read it as a pass.

    The timeline is seeked to its END before measuring: compositions animate
    in, so sampling at t=0 measures a frame that is still empty by design.
    """
    import tempfile

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # playwright not installed -> infra, not a defect
        logger.info('[MotionVideo] fill gate unavailable (playwright): %s', e)
        return None

    try:
        import chromium_env
        chromium_env.ensure_chromium_env(os.environ)
    except Exception as e:
        # Not fatal: a system Chromium may still resolve without the shim.
        logger.debug('[MotionVideo] chromium_env shim unavailable: %s', e)

    width, height = _frame_size(html)
    tmp_path = ''
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False,
                                         encoding='utf-8') as fh:
            fh.write(html)
            tmp_path = fh.name
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={'width': width, 'height': height})
                page.goto('file://' + tmp_path, wait_until='load',
                          timeout=timeout_ms)
                page.wait_for_timeout(350)
                # Seek every registered timeline to its end -- the fullest frame.
                page.evaluate(
                    '() => { const t = window.__timelines || {};'
                    ' for (const k in t) { try { t[k].progress(1).pause(); }'
                    ' catch (e) {} } }')
                page.wait_for_timeout(150)
                raw = page.evaluate(_MEASURE_JS)
            finally:
                browser.close()
    except Exception as e:
        logger.info('[MotionVideo] fill measurement failed: %s', e)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError as e:
                logger.debug('fill: temp cleanup failed: %s', e)

    if not raw:
        return None
    frame_h = float(raw['H']) or 1.0
    # Only ink INSIDE the frame is visible -- the renderer clips overflow, so
    # an unclamped span can exceed 100% by measuring pixels nobody ever sees.
    vis_top = max(0.0, min(float(raw['top']), frame_h))
    vis_bottom = max(0.0, min(float(raw['bottom']), frame_h))
    return {
        'span': max(0.0, vis_bottom - vis_top) / frame_h,
        'top_dead': vis_top / frame_h,
        'bottom_dead': max(0.0, frame_h - vis_bottom) / frame_h,
        'nodes': int(raw['n']),
    }


def findings_for_fill(m: dict | None) -> list[str]:
    """Turn a :func:`measure_fill` result into findings. Pure — no browser.

    Split out of :func:`check_composition_fill` so a caller that ALREADY holds
    a measurement can derive the findings without paying a second headless
    Chrome boot (~1.2s per scene). The engine needs both the findings (for the
    quality axis) and the raw numbers (for per-scene telemetry) from ONE
    measurement; before this split the only way to get both was to measure
    twice, which is a real cost on a multi-scene film and — worse — could
    return two DIFFERENT verdicts for one composition if a boot flaked.

    ``None`` (measurement not takeable) yields no findings, matching the
    infrastructure-is-not-a-defect rule the CLI gates already follow.
    """
    if not m:
        return []
    findings: list[str] = []
    if m['span'] < MIN_VERTICAL_SPAN:
        findings.append(
            f'vertical span is only {m["span"] * 100:.0f}% of the frame '
            f'(minimum {MIN_VERTICAL_SPAN * 100:.0f}%) — the composition '
            f'occupies a fraction of a tall frame. Distribute the beat across '
            f'the full height (space-between column, or add a supporting '
            f'element) rather than centring one block.')
    if m['bottom_dead'] > MAX_BOTTOM_DEAD_BAND:
        findings.append(
            f'{m["bottom_dead"] * 100:.0f}% of the frame height is empty '
            f'below the last element (maximum '
            f'{MAX_BOTTOM_DEAD_BAND * 100:.0f}%) — the frame reads as '
            f'bottom-weighted dead space. Extend the layout downward or '
            f'rebalance the vertical rhythm.')
    return findings


def check_composition_fill(html: str, *, timeout_ms: int = 20000) -> list[str]:
    """Reject a composition that leaves most of a tall frame empty.

    Returns finding strings for the author's repair loop (empty = clean).
    A measurement that could not be taken returns EMPTY: an absent browser is
    an infrastructure outcome, and charging it to the composition would
    degrade every scene on a host without Chromium -- the same rule the CLI
    gates already follow for ``env_missing``.
    """
    return findings_for_fill(measure_fill(html, timeout_ms=timeout_ms))
