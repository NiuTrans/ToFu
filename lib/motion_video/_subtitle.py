"""lib/motion_video/_subtitle.py — Burn-in subtitle geometry (single source).

Why this module exists (measured 2026-07-28, not hypothesized). The pipeline
burned its sidecar SRT with **no style at all**: every call site passed only
``fontsdir``, so ``force_style`` was never populated by anybody. A bare SRT
carries no ``[Script Info]``, so libass falls back to its ``384x288``
reference resolution and scales the default 16pt style by ``frame_height/288``
— on a 1440px frame that is a 5x blow-up. Combined with the second defect
below, a real shipped cue measured an ink bounding box of ``x[0..1079]`` on a
1080px frame: clipped at BOTH edges.

Two independent root causes, both fixed here:

1. **No geometry contract.** ``force_style`` can only override ``[V4+ Styles]``
   fields — it CANNOT set ``PlayResX`` / ``PlayResY``, which live in
   ``[Script Info]``. Measured: ``force_style='FontSize=10'`` still produced
   ``x[0..1079]``, and the ``subtitles`` filter's ``original_size`` option
   changed nothing either. The ONLY mechanism that actually binds libass to
   the real frame geometry is emitting a real ``.ass`` document with an
   explicit ``PlayResX``/``PlayResY`` header. So that is what
   :func:`build_ass` does; the SRT remains the user-facing sidecar.

2. **libass does not wrap CJK.** It breaks lines at spaces / hyphens only.
   Our cues are whole spoken sentences with ZERO spaces (one shipped cue was
   53 CJK chars), so there is no break opportunity and the line runs off both
   edges. Measured across ``WrapStyle`` 0, 1 and 2: all three produced the
   identical clipped ``x[0..1079]``. Therefore the text MUST be pre-wrapped
   by us, before libass sees it.

**Wrapping uses real font metrics, never a guessed width ratio.** A naive
east-asian-width model (CJK=1.0, Latin=0.5) under-predicts badly: measured
Latin advance on this host's CJK face is 0.77 of full-width, not 0.5, so that
model predicted 912px for a line whose real ink was 1054px — still an
overflow. Instead :func:`wrap_line` measures candidate lines with FreeType
(the same font file libass resolves) and breaks on the real advance width.

The advance width is a deliberate CONSERVATIVE bound: measured
``ink / advance = 0.6909`` with ``sd = 0.0007`` across ``Fontsize`` 32→64
(scale-invariant, because ``Fontsize`` is the em box while ink is the drawn
glyph). Budgeting the full advance therefore leaves ~30% headroom, so a line
that fits the budget cannot overflow even if the font resolution shifts.
"""

from __future__ import annotations

import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['SubtitleStyle', 'style_for_frame', 'wrap_line', 'build_ass',
           'safe_box', 'measure_advance']

#: Share of frame WIDTH kept clear on each side. The burned line must live
#: inside ``[margin, width - margin]``; the real-render guard asserts it.
_SIDE_MARGIN_SHARE = 1 / 15.0
#: Share of frame HEIGHT between the frame bottom and the subtitle baseline.
_BOTTOM_MARGIN_SHARE = 0.055
#: Subtitle cap height as a share of frame HEIGHT. 1440 * 0.0306 ~= 44px,
#: the size the geometry above was calibrated at.
_FONT_SHARE = 0.0306
#: Outline thickness relative to the font size (readability over any frame).
_OUTLINE_SHARE = 1 / 14.0
#: Hard ceiling on wrapped lines per cue — beyond this the cue is a paragraph
#: and would cover the composition, so it is reported rather than silently
#: stacked over the whole frame.
MAX_LINES_PER_CUE = 3


class SubtitleStyle:
    """Resolved burn-in geometry for ONE frame size.

    Every field is derived from the frame, so a 1080x1920 or 1920x1080 job
    gets proportionate type instead of the 384x288 default blow-up.
    """

    __slots__ = ('width', 'height', 'font_px', 'margin_x', 'margin_v',
                 'outline', 'font_name', 'font_file')

    def __init__(self, width: int, height: int, *, font_name: str = '',
                 font_file: str = ''):
        self.width = int(width)
        self.height = int(height)
        self.font_px = max(18, round(self.height * _FONT_SHARE))
        self.margin_x = max(24, round(self.width * _SIDE_MARGIN_SHARE))
        self.margin_v = max(16, round(self.height * _BOTTOM_MARGIN_SHARE))
        self.outline = max(2, round(self.font_px * _OUTLINE_SHARE))
        self.font_name = font_name or 'Sans'
        self.font_file = font_file

    @property
    def usable_px(self) -> int:
        """Horizontal room a wrapped line may occupy."""
        return self.width - 2 * self.margin_x

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (f'SubtitleStyle({self.width}x{self.height}, '
                f'font_px={self.font_px}, margin_x={self.margin_x}, '
                f'font={self.font_name!r})')


def safe_box(width: int, height: int) -> tuple[int, int]:
    """``(left, right)`` x-bounds burned ink must stay within.

    The real-render guard and the wrapper read the SAME numbers from here, so
    the check and the thing it checks can never drift apart.
    """
    st = SubtitleStyle(width, height)
    return st.margin_x, width - st.margin_x


def _resolve_cjk_font() -> tuple[str, str]:
    """``(family, file)`` of a font that actually covers CJK, via fontconfig.

    Returns ``('Sans', '')`` when nothing resolves — the caller then emits a
    style naming ``Sans`` and lets libass do its own fallback, which is the
    pre-existing behaviour rather than a new failure mode.
    """
    import subprocess
    from lib.motion_video._env import build_render_env
    try:
        # charset=6c49 is U+6C49 (汉) — matches only faces with real CJK
        # coverage, so we never pin a Latin-only face that would tofu-box.
        out = subprocess.run(
            ['fc-match', '-s', 'Sans:charset=6c49', '-f', '%{file}\t%{family}\n'],
            capture_output=True, text=True, timeout=20,
            env=build_render_env())
    except Exception as e:
        logger.debug('[Subtitle] fc-match unavailable: %s', e)
        return 'Sans', ''
    for line in (out.stdout or '').splitlines():
        if '\t' not in line:
            continue
        path, family = line.split('\t', 1)
        if not os.path.isfile(path):
            continue
        # fc-match -s returns the ranked list; the first entry that really
        # carries the glyph wins. Families come comma-separated.
        fam = family.split(',')[0].strip()
        if fam:
            return fam, path
    return 'Sans', ''


def style_for_frame(width: int, height: int) -> SubtitleStyle:
    """The style contract for a frame size (resolves a CJK-capable face)."""
    family, path = _resolve_cjk_font()
    st = SubtitleStyle(width, height, font_name=family, font_file=path)
    logger.info('[Subtitle] style for %dx%d: font=%r size=%dpx margin_x=%dpx '
                'usable=%dpx', width, height, st.font_name, st.font_px,
                st.margin_x, st.usable_px)
    return st


def measure_advance(text: str, style: SubtitleStyle):
    """Advance width of ``text`` in px at the style's size, or None.

    None means FreeType could not measure (no font file / PIL absent); the
    caller falls back to a character-count budget rather than failing the
    burn.
    """
    if not style.font_file:
        return None
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(style.font_file, style.font_px)
        return font.getlength(text)
    except Exception as e:
        logger.debug('[Subtitle] advance measure failed: %s', e)
        return None


def _fallback_display_width(text: str, style: SubtitleStyle) -> float:
    """Character-class width estimate, used only when FreeType is unavailable.

    Deliberately PESSIMISTIC: the measured Latin advance on this host's CJK
    face is 0.77 of full-width, so 0.5 (the textbook east-asian value) would
    under-predict and let a line overflow. We use the measured ratio.
    """
    import unicodedata
    units = sum(1.0 if unicodedata.east_asian_width(c) in ('W', 'F') else 0.77
                for c in text)
    return units * style.font_px


def _break_oversized(token: str, budget: float, width) -> list[str]:
    """Split a single token that cannot fit ``budget`` on any line.

    Reached by a pathological unbroken run (a long URL, a 400-char word, CJK
    with no punctuation). Returns the chunks in order; the LAST chunk is the
    remainder that may still accept more text.

    This must be reachable for a token that STARTS a line, not only for one
    that overflows an existing line — that asymmetry was a real bug: a
    400-char single token was emitted unbroken because the "flush current
    line" branch never fired for it.
    """
    chunks: list[str] = []
    rest = token
    while width(rest) > budget and len(rest) > 1:
        lo, hi = 1, len(rest)
        while lo < hi:                      # widest prefix that still fits
            mid = (lo + hi + 1) // 2
            if width(rest[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        chunks.append(rest[:lo])
        rest = rest[lo:]
    chunks.append(rest)
    return chunks


def wrap_line(text: str, style: SubtitleStyle) -> list[str]:
    """Break ``text`` into lines that fit ``style.usable_px``.

    Uses real FreeType advance widths (the same font file libass resolves) so
    mixed CJK/Latin/digit cues wrap correctly — a character-count budget is
    wrong for mixed text and was measured overflowing by 118px.

    Latin words are kept whole (breaking mid-word is a legibility defect);
    CJK may break between any two characters, which is correct for Chinese
    typesetting and is the only way an unspaced 53-char sentence can wrap.
    A token too long to fit ANY line is split rather than allowed to overflow.
    """
    text = (text or '').strip()
    if not text:
        return []
    budget = style.usable_px

    def width(s: str) -> float:
        got = measure_advance(s, style)
        return got if got is not None else _fallback_display_width(s, style)

    # Tokenise so a Latin word / number stays atomic while CJK stays
    # per-character breakable.
    tokens = re.findall(r'[A-Za-z0-9][A-Za-z0-9._+\-]*|\s+|.', text)
    lines: list[str] = []
    cur = ''
    for tok in tokens:
        if tok.isspace():
            # A space only matters between words; never start a line with it.
            if cur:
                cur += ' '
            continue
        # A token that cannot fit a line on its own must be split FIRST —
        # independent of whether a line is already open.
        if width(tok) > budget:
            if cur.strip():
                lines.append(cur.rstrip())
                cur = ''
            pieces = _break_oversized(tok, budget, width)
            lines.extend(pieces[:-1])
            cur = pieces[-1]
            continue
        cand = cur + tok
        if cur and width(cand) > budget:
            lines.append(cur.rstrip())
            cur = tok
        else:
            cur = cand
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _ass_escape(text: str) -> str:
    """Escape a cue for the ASS Dialogue field (which is newline-delimited)."""
    return (text.replace('\\', '\\\\')
                .replace('{', '\\{')
                .replace('}', '\\}')
                .replace('\r', '')
                .replace('\n', ' '))


def _ass_time(seconds: float) -> str:
    """``H:MM:SS.cc`` — the ASS timestamp format (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    cs_total = int(round(seconds * 100))
    h, rem = divmod(cs_total, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f'{h:d}:{m:02d}:{s:02d}.{cs:02d}'


def build_ass(cues, width: int, height: int, *,
              style: SubtitleStyle | None = None) -> tuple[str, list[str]]:
    """Render cues into a full ASS document bound to the real frame geometry.

    ``cues`` is an iterable of ``(start_s, end_s, text)``. Returns
    ``(ass_text, warnings)``; a warning is emitted for any cue that needed
    more than :data:`MAX_LINES_PER_CUE` lines, since such a cue covers the
    composition it is supposed to caption.
    """
    st = style or style_for_frame(width, height)
    warnings: list[str] = []
    events: list[str] = []
    for idx, (start, end, text) in enumerate(cues, 1):
        lines = wrap_line(text, st)
        if not lines:
            continue
        if len(lines) > MAX_LINES_PER_CUE:
            warnings.append(
                f'cue {idx} needs {len(lines)} lines at {st.font_px}px '
                f'({len(text)} chars) — over the {MAX_LINES_PER_CUE}-line '
                f'budget, so it covers the frame it captions; shorten the '
                f'narration for that scene')
        body = '\\N'.join(_ass_escape(ln) for ln in lines)
        events.append(
            f'Dialogue: 0,{_ass_time(start)},{_ass_time(end)},'
            f'Default,,0,0,0,,{body}')

    head = (
        '[Script Info]\n'
        'ScriptType: v4.00+\n'
        f'PlayResX: {st.width}\n'
        f'PlayResY: {st.height}\n'
        'WrapStyle: 2\n'          # we pre-wrapped; forbid libass re-flowing
        'ScaledBorderAndShadow: yes\n'
        'YCbCr Matrix: TV.709\n'
        '\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, '
        'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, '
        'Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, '
        'MarginR, MarginV, Encoding\n'
        f'Style: Default,{st.font_name},{st.font_px},&H00FFFFFF,&H00000000,'
        f'&HA0000000,0,0,0,0,100,100,0,0,1,{st.outline},0,2,'
        f'{st.margin_x},{st.margin_x},{st.margin_v},1\n'
        '\n'
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, '
        'Effect, Text\n'
    )
    if warnings:
        for w in warnings:
            logger.warning('[Subtitle] %s', w)
    return head + '\n'.join(events) + '\n', warnings
