"""lib/motion_video/_template.py — Zero-LLM scene composition generator.

The headless engine's composition fallback: a clean kinetic-type card per
scene (headline slide-in → hold → gentle settle) built on the same contract
as ``guide/skeleton.html``. Output is deterministic and passes
:func:`lib.motion_video._gates.check_composition_html` by construction
(HTML-escaped text, one paused GSAP timeline, no banned patterns).

The chat-agent path authors creative compositions itself; this template is
the floor that lets a fully-automatic SRT → narrated-video run succeed.

**Three separate fields, three separate jobs** (owner 2026-07-27). A scene
carries:

  * ``text``      — the SPOKEN narration (TTS input + sidecar SRT). May be
                    long; it is never drawn on the frame.
  * ``on_screen`` — the ON-FRAME caption. Bounded by :func:`on_screen_capacity`,
                    which is derived from the real frame geometry.
  * ``visual``    — ART DIRECTION for the per-scene author
                    (:mod:`lib.motion_video._scene_author`) and the reserved
                    ``'sources'`` marker for the end card. NEVER drawn as the
                    headline.

Collapsing any two of those onto one string is what produced the 1968-char
headline at 46px that overflowed a 1440px frame — the template reads
``on_screen`` and only falls back to ``text`` for legacy storyboards.
"""

from __future__ import annotations

import html as _html

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['render_scene_html', 'on_screen_capacity', 'fit_font_px',
           'scene_on_screen', 'FONT_PX_STEPS', 'MIN_FONT_PX', 'CAPTION_FONT_PX',
           'TEMPLATE_MARKER', 'is_template_composition', 'matches_template']

#: Stamped into every template composition so a FALLBACK card is
#: distinguishable from an authored one on disk. Without it the engine's
#: resume path (which compared only ``data-duration``) adopted a card left by
#: a transient failure and pinned that scene to the gradient forever.
TEMPLATE_MARKER = 'data-tofu-composition="template-fallback"'


def is_template_composition(html: str) -> bool:
    """True when ``html`` is the zero-LLM fallback card rather than authored."""
    return TEMPLATE_MARKER in (html or '')


def _normalise_for_compare(html: str) -> str:
    """Collapse whitespace and drop the marker so pre/post-marker cards match."""
    import re as _re
    out = (html or '').replace(TEMPLATE_MARKER, '')
    return _re.sub(r'\s+', ' ', out).strip()


def matches_template(html: str, scene: dict, *, width: int = 1080,
                     height: int = 1440, duration: float | None = None,
                     scene_index: int = 1, total_scenes: int = 1,
                     theme=None) -> bool:
    """True when ``html`` IS this scene's fallback card, marker or not.

    :func:`is_template_composition` only recognises cards that carry
    :data:`TEMPLATE_MARKER` — and the marker was added on 2026-07-29, so every
    fallback card written before that is invisible to it. That gap is not
    cosmetic: measured on the film that prompted this whole effort, scene-004's
    2,398-byte gradient card predates the marker, so ``_existing_composition``
    adopted it as a finished authored composition and the scene was pinned to
    the fallback FOREVER — re-running the job could never retry its authoring,
    which is exactly the failure the marker was introduced to prevent.

    The test is exact rather than heuristic: the template is a deterministic
    function of (scene, geometry, duration, index, total), so we RE-RENDER it
    and compare. No fingerprint to keep in sync, no false positives on a
    genuinely minimal authored scene, and any future change to the template is
    tracked automatically because the comparison uses the template itself.
    """
    if not html:
        return False
    if is_template_composition(html):
        return True
    try:
        rebuilt = render_scene_html(scene, width=width, height=height,
                                    duration=duration,
                                    scene_index=scene_index,
                                    total_scenes=total_scenes, theme=theme)
    except Exception as e:
        logger.warning('[MotionVideo] template re-render for comparison '
                       'failed: %s', e)
        return False
    return _normalise_for_compare(html) == _normalise_for_compare(rebuilt)

#: Candidate headline sizes, largest first. The chosen size is the biggest one
#: whose measured capacity still holds the caption.
FONT_PX_STEPS = (120, 96, 76, 60, 46)
#: The floor — below this the card stops reading as a title card.
MIN_FONT_PX = FONT_PX_STEPS[-1]
#: The size a CAPTION is expected to read at. The 46px floor exists so an
#: over-long legacy caption still renders; it is NOT a licence to author at
#: that size. A caption that only fits at the floor is a paragraph, so the
#: authoring budget is the capacity at this title size instead.
CAPTION_FONT_PX = 76

#: Fraction of the frame the headline box may occupy. Mirrors the CSS below
#: (``max-width`` = 84% of frame width); the height share leaves room for the
#: scene tag, the text-shadow bleed and top/bottom breathing space.
_HEADLINE_WIDTH_SHARE = 0.84
_HEADLINE_HEIGHT_SHARE = 0.62
#: Matches ``line-height: 1.4`` in the stylesheet.
_LINE_HEIGHT = 1.4


def on_screen_capacity(width: int = 1080, height: int = 1440,
                       font_px: int = MIN_FONT_PX) -> int:
    """Max caption characters that fit the headline box at ``font_px``.

    Derived from the SAME geometry the stylesheet uses (84% width box,
    ``line-height: 1.4``), counting every glyph as full-width — CJK is
    full-width and Latin is narrower, so the estimate never over-promises.

    This is the single source of truth for "does this caption fit": the
    template picks a font size with it and
    :func:`lib.motion_video._gates.check_scene_budget` rejects captions that
    exceed it, so the gate and the renderer can never disagree.
    """
    font_px = max(1, int(font_px))
    cols = int((width * _HEADLINE_WIDTH_SHARE) // font_px)
    lines = int((height * _HEADLINE_HEIGHT_SHARE) // (font_px * _LINE_HEIGHT))
    return max(0, cols * lines)


def fit_font_px(text: str, width: int = 1080, height: int = 1440) -> int:
    """Largest step whose capacity holds ``text``; :data:`MIN_FONT_PX` if none.

    Returning the floor for over-long text is deliberate: the renderer must
    still produce a frame, and the budget gate is what refuses to let such a
    caption reach the renderer in the first place.
    """
    n = len(text or '')
    for px in FONT_PX_STEPS:
        if n <= on_screen_capacity(width, height, px):
            return px
    return MIN_FONT_PX


def scene_on_screen(scene: dict) -> str:
    """The caption to draw: ``on_screen``, falling back to ``text``.

    ``visual`` is NEVER consulted — it holds art direction (and the reserved
    ``'sources'`` marker), so reading it here would render the literal string
    ``sources`` as the end card's headline.
    """
    if not isinstance(scene, dict):
        return ''
    caption = str(scene.get('on_screen') or '').strip()
    if caption:
        return caption
    return str(scene.get('text') or '').strip()


_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <title>{scene_id}</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ margin: 0; width: {width}px; height: {height}px; overflow: hidden; background: #000; }}
      /* Only fonts the renderer auto-resolves may be NAMED here: naming an
         absent face (PingFang SC / Noto Sans CJK SC) does not get you that
         face, it gets a silent fallback to whatever fontconfig has — on a
         host whose only CJK face is a SERIF, this sans stack rendered serif.
         CJK coverage therefore comes from fontconfig fallback. */
      body {{ font-family: Inter, system-ui, sans-serif; }}
      #root {{ position: relative; width: {width}px; height: {height}px; overflow: hidden; }}
      .bgfill {{ position: absolute; inset: 0; background: {background}; }}
      .clip {{ position: absolute; inset: 0; display: grid; place-items: center; }}
      .headline {{
        font-size: {font_px}px; font-weight: 800; color: {ink}; letter-spacing: -1px;
        max-width: {max_width}px; text-align: center; line-height: 1.4;
        text-shadow: 0 4px 32px rgba(0,0,0,.45);
      }}
      .tag {{
        position: absolute; top: 48px; left: 56px;
        font-size: 30px; font-weight: 600; color: {muted};
        letter-spacing: 4px;
      }}
      .accentrule {{
        position: absolute; top: 96px; left: 56px; width: 64px; height: 6px;
        background: {accent};
      }}
    </style>
  </head>
  <body>
    <div
      id="root"
      data-composition-id="main"
      data-tofu-composition="template-fallback"
      data-start="0"
      data-duration="{duration}"
      data-width="{width}"
      data-height="{height}"
    >
      <div class="bgfill"></div>
      <section id="card-1" class="clip" data-start="0" data-duration="{duration}" data-track-index="1">
        <div class="tag" id="scenetag">{tag}</div>
        <div class="accentrule"></div>
        <h1 class="headline" id="headline">{headline}</h1>
      </section>
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      tl.from('#headline', {{ y: 56, opacity: 0, duration: 0.7, ease: 'power3.out' }}, 0.15);
      tl.from('#scenetag', {{ opacity: 0, duration: 0.5, ease: 'power2.out' }}, 0.1);
      window.__timelines['main'] = tl;
    </script>
  </body>
</html>
"""

_GRADIENTS = (
    'linear-gradient(160deg, #0b0f14 0%, #14243a 60%, #1d3a5f 100%)',
    'linear-gradient(160deg, #140b12 0%, #2a1430 60%, #3f1d4d 100%)',
    'linear-gradient(160deg, #0b1410 0%, #14301f 60%, #1d4d33 100%)',
    'linear-gradient(160deg, #14120b 0%, #302714 60%, #4d3d1d 100%)',
)


def render_scene_html(scene: dict, *, width: int = 1080, height: int = 1440,
                      duration: float | None = None,
                      scene_index: int = 1, total_scenes: int = 1,
                      theme=None) -> str:
    """Render one scene dict into a composition HTML.

    Draws ``scene['on_screen']`` (falling back to ``text`` for legacy
    storyboards) at the largest font size whose measured capacity holds it.
    ``scene['visual']`` is art direction and is never drawn.

    ``theme`` (design_sys Theme) replaces the four-gradient rotation with the
    FILM's palette: solid theme ground, ink headline, muted tag, one accent
    rule. Even the fallback then carries the film's visual system instead of
    advertising that it is the fallback.
    """
    caption = scene_on_screen(scene)
    headline = _html.escape(caption) if caption else '…'
    dur = duration if duration and duration > 0 else (
        float(scene.get('end') or 0) - float(scene.get('start') or 0))
    dur = max(0.5, round(float(dur), 3))
    scene_id = str(scene.get('id') or f'scene-{scene_index:03d}')
    font_px = fit_font_px(caption, width, height)
    if len(caption) > on_screen_capacity(width, height, font_px):
        # The budget gate is supposed to stop this upstream; if we still get
        # here the frame WILL overflow, so say so loudly rather than shipping
        # a silently clipped card.
        logger.warning('[MotionVideo] %s caption is %d chars but only %d fit '
                       'at %dpx in %dx%d — the frame will overflow',
                       scene_id, len(caption),
                       on_screen_capacity(width, height, font_px), font_px,
                       width, height)
    if theme is not None:
        c = theme.colors
        background = c['bg']
        ink = c['ink']
        muted = c['muted']
        accent = c['accent']
    else:
        background = _GRADIENTS[(scene_index - 1) % len(_GRADIENTS)]
        ink = '#fff'
        muted = 'rgba(255,255,255,.55)'
        accent = 'rgba(255,255,255,.0)'
    return _TEMPLATE.format(
        width=width, height=height, duration=dur,
        scene_id=_html.escape(scene_id),
        headline=headline,
        tag=_html.escape(f'{scene_index:02d} / {total_scenes:02d}'),
        font_px=font_px,
        max_width=int(width * _HEADLINE_WIDTH_SHARE),
        background=background,
        ink=ink, muted=muted, accent=accent,
    )
