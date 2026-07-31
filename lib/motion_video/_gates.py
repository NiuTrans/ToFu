"""lib/motion_video/_gates.py — Zero-LLM validation gates.

Every gate in the motion-video pipeline that does NOT need a model lives
here, so the agent's self-repair loop has deterministic, cheap checks to
bounce against (the same philosophy as the paper-podcast script gates):

  * :func:`check_storyboard` — scenes.json structural + timeline gates:
    required fields, monotonic contiguity, full coverage of the SRT span,
    duration-sum equality (±tolerance), non-empty scene text.
  * :func:`check_composition_html` — the HyperFrames composition contract,
    statically: root ``data-*`` attributes present, ``window.__timelines``
    key matching ``data-composition-id``, a paused GSAP timeline, and the
    determinism ban-list (render-time clocks / unseeded random / infinite
    repeats / rAF / setInterval).
  * :func:`probe_video` / :func:`verify_spec` — post-render media spec
    verification via ffprobe (with an ``ffmpeg -i`` fallback): codec,
    resolution, fps, duration, silence.

Each function returns a list of human-readable error strings (empty = pass)
so the agent can feed them straight back into a repair prompt.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['check_storyboard', 'check_composition_html', 'probe_video',
           'verify_spec', 'check_scene_budget', 'check_text_fidelity',
           'visible_text', 'NARRATION_CHARS_PER_SECOND']

# ── Storyboard gates ──────────────────────────────────────

_SCENE_REQUIRED = ('id', 'start', 'end', 'text')

#: Narration pace used to test whether a scene's spoken text fits its slot.
#: Deliberately the same scale the paper/podcast chains estimate with, so a
#: "fits" verdict here matches what TTS actually produces.
NARRATION_CHARS_PER_SECOND = 4.2


def check_scene_budget(scenes, *, width: int = 1080, height: int = 1440,
                       max_scene_s: float = 15.0,
                       chars_per_second: float = NARRATION_CHARS_PER_SECOND,
                       narration: bool = True) -> list[str]:
    """Reject storyboards whose text does not fit its frame or its time.

    ``check_storyboard`` only validates the TIMELINE (contiguity, coverage,
    duration sum) — a storyboard can pass it while every scene is an
    unreadable wall of text, which is exactly how a 1968-char headline
    reached a 1080x1440 frame. This gate closes the three holes that made
    that possible:

    1. **Saturation** — a scene sitting exactly on ``max_scene_s`` means the
       duration was CLAMPED, i.e. the builder silently swallowed a text/time
       mismatch instead of re-cutting the storyboard. Clamping is not a
       rounding artefact, it is a lost error, so it fails here.
    2. **Caption capacity** — ``on_screen`` must fit the headline box at the
       minimum readable font size, measured with the SAME geometry the
       template renders with (:func:`lib.motion_video._template.on_screen_capacity`).
    3. **Narration fit** — spoken ``text`` must be utterable within the
       scene's own duration at ``chars_per_second``. Without this a 2-minute
       film can carry an hour of narration and ``loose`` alignment silently
       stretches the film to match.

    Returns a list of human-readable errors (empty = pass).
    """
    from lib.motion_video._template import (CAPTION_FONT_PX, MIN_FONT_PX,
                                            on_screen_capacity,
                                            scene_on_screen)

    errors: list[str] = []
    if not isinstance(scenes, list) or not scenes:
        return ['scenes must be a non-empty list']

    capacity = on_screen_capacity(width, height, MIN_FONT_PX)
    caption_cap = on_screen_capacity(width, height, CAPTION_FONT_PX)
    for i, sc in enumerate(scenes):
        label = sc.get('id', f'#{i + 1}') if isinstance(sc, dict) else f'#{i + 1}'
        if not isinstance(sc, dict):
            errors.append(f'scene {label}: not an object')
            continue
        start, end = _num(sc.get('start')), _num(sc.get('end'))
        if start is None or end is None or end <= start:
            continue  # shape errors are check_storyboard's job
        dur = end - start

        if dur >= max_scene_s - 1e-6:
            errors.append(
                f'scene {label}: duration {dur:.3f}s is saturated at the '
                f'{max_scene_s}s ceiling — the storyboard was clamped instead '
                f'of re-cut; split this beat or shorten its text')

        caption = scene_on_screen(sc)
        spoken_text = str(sc.get('text') or '').strip()
        if not caption:
            errors.append(f'scene {label}: no on_screen caption '
                          '(and no text to fall back to)')
        elif len(caption) > capacity:
            errors.append(
                f'scene {label}: on_screen caption is {len(caption)} chars but '
                f'only {capacity} fit a {width}x{height} frame at {MIN_FONT_PX}px '
                f'— write a caption, do not paste the narration')
        elif caption == spoken_text and len(caption) > caption_cap:
            # The capacity check alone cannot see this: a caption under the
            # 46px floor still passes while being the narration shown twice.
            # A caption is a COMPRESSION of the beat, so an over-long verbatim
            # copy means the field was never authored.
            errors.append(
                f'scene {label}: on_screen duplicates the narration verbatim '
                f'({len(caption)} chars, over the {caption_cap}-char caption '
                f'budget at {CAPTION_FONT_PX}px) — captions must condense the '
                f'beat, not repeat it')

        if narration and sc.get('spoken', True):
            spoken = str(sc.get('text') or '')
            need = len(spoken) / chars_per_second if chars_per_second > 0 else 0.0
            if need > dur + 0.5:
                errors.append(
                    f'scene {label}: narration needs ~{need:.1f}s at '
                    f'{chars_per_second} chars/s but the scene is only '
                    f'{dur:.1f}s — the film would stretch to fit')
    return errors


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError) as _e:
        logger.debug('num: unexpected type/unparseable (%s)', _e)
        return None


def check_storyboard(scenes, span: tuple[float, float],
                     tol: float = 0.1) -> list[str]:
    """Validate a scenes.json list against the SRT span.

    Args:
        scenes: list of dicts with id/start/end/text (seconds, float).
        span: ``(first_start, last_end)`` from :func:`lib.motion_video._srt.total_span`.
        tol: coverage / contiguity tolerance in seconds (design contract: ±0.1s).

    Returns a list of error strings (empty when the storyboard is valid).
    """
    errors: list[str] = []
    if not isinstance(scenes, list) or not scenes:
        return ['scenes must be a non-empty list']
    span_start, span_end = span
    span_dur = span_end - span_start

    total = 0.0
    prev_end: float | None = None
    for i, sc in enumerate(scenes):
        label = sc.get('id', f'#{i + 1}') if isinstance(sc, dict) else f'#{i + 1}'
        if not isinstance(sc, dict):
            errors.append(f'scene {label}: not an object')
            continue
        for key in _SCENE_REQUIRED:
            if key not in sc:
                errors.append(f'scene {label}: missing field {key!r}')
        start, end = _num(sc.get('start')), _num(sc.get('end'))
        if start is None or end is None:
            errors.append(f'scene {label}: start/end must be numbers (seconds)')
            continue
        if end <= start:
            errors.append(f'scene {label}: end ({end:.3f}) must be after start ({start:.3f})')
            continue
        if not str(sc.get('text') or '').strip():
            errors.append(f'scene {label}: text is empty')
        if prev_end is not None and abs(start - prev_end) > tol:
            kind = 'gap' if start > prev_end else 'overlap'
            errors.append(f'scene {label}: {kind} vs previous scene '
                          f'(prev end {prev_end:.3f}, this start {start:.3f})')
        prev_end = end
        total += end - start

    if isinstance(scenes[0], dict):
        first_start = _num(scenes[0].get('start'))
        if first_start is not None and abs(first_start - span_start) > tol:
            errors.append(f'first scene starts at {first_start:.3f} but SRT starts '
                          f'at {span_start:.3f} (tol {tol}s)')
    if isinstance(scenes[-1], dict):
        last_end = _num(scenes[-1].get('end'))
        if last_end is not None and abs(last_end - span_end) > tol:
            errors.append(f'last scene ends at {last_end:.3f} but SRT ends '
                          f'at {span_end:.3f} (tol {tol}s)')
    if prev_end is not None and abs(total - span_dur) > tol:
        errors.append(f'scene durations sum to {total:.3f}s but SRT span is '
                      f'{span_dur:.3f}s (tol {tol}s)')
    return errors


# ── Composition static gates ──────────────────────────────

_COMP_ID_RE = re.compile(r'data-composition-id="([^"]+)"')
_DURATION_RE = re.compile(r'data-duration="([0-9.]+)"')
_WIDTH_RE = re.compile(r'data-width="(\d+)"')
_HEIGHT_RE = re.compile(r'data-height="(\d+)"')
_PAUSED_TL_RE = re.compile(r'gsap\.timeline\(\s*\{[^}]*paused\s*:\s*true')

#: (pattern, label) — determinism ban-list from the HyperFrames contract.
_BANNED = (
    (re.compile(r'\bDate\.now\s*\('), 'Date.now() (render-time clock)'),
    (re.compile(r'\bperformance\.now\s*\('), 'performance.now() (render-time clock)'),
    (re.compile(r'\bMath\.random\s*\('), 'Math.random() (unseeded randomness)'),
    (re.compile(r'repeat\s*:\s*-1'), 'repeat: -1 (infinite loop breaks seek)'),
    (re.compile(r'\brequestAnimationFrame\s*\('), 'requestAnimationFrame (frame-chained state)'),
    (re.compile(r'\bsetInterval\s*\('), 'setInterval (wall-clock state)'),
)


def check_composition_html(html: str) -> list[str]:
    """Statically validate one HyperFrames composition HTML string."""
    errors: list[str] = []
    if not html or not html.strip():
        return ['composition HTML is empty']

    # Banned-pattern scan runs on CODE, not prose: strip HTML comments and
    # JS block comments first so a contract note mentioning e.g. `repeat:-1`
    # doesn't self-trip the gate.
    code = re.sub(r'<!--.*?-->', '', html, flags=re.S)
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.S)

    comp = _COMP_ID_RE.search(code)
    if not comp:
        errors.append('missing data-composition-id on the composition root')
    if not _DURATION_RE.search(code):
        errors.append('missing data-duration on the composition root')
    if not _WIDTH_RE.search(code) or not _HEIGHT_RE.search(code):
        errors.append('missing data-width / data-height on the composition root '
                      '(the root must be explicitly sized)')

    if comp:
        key_re = re.compile(
            r'window\.__timelines\[\s*[\'"]' + re.escape(comp.group(1)) + r'[\'"]\s*\]')
        if '__timelines' not in code:
            errors.append('no window.__timelines registration found')
        elif not key_re.search(code):
            errors.append(f'window.__timelines key must equal data-composition-id '
                          f'({comp.group(1)!r})')
    if 'gsap.' in code and not _PAUSED_TL_RE.search(code):
        errors.append('GSAP timeline must be created with { paused: true }')

    for rx, label in _BANNED:
        if rx.search(code):
            errors.append(f'determinism violation: {label}')
    return errors


# ── Text fidelity ─────────────────────────────────────────
#
# WHY THIS GATE EXISTS (measured, not hypothesised): a real authored scene
# rendered the eyebrow 「极极致耐用测试」 while its beat said 「耐用性」. The
# frame passed EVERY existing gate — lint (fonts), validate (runtime errors +
# WCAG contrast) and inspect (overflow) all check that a frame is
# WELL-FORMED; none of them has an opinion on whether it is RIGHT. A
# duplicated character today is a wrong number or a garbled product name
# tomorrow, so "ships green while visibly wrong to a human" needs its own
# check rather than a copy-edit pass.

#: Two identical adjacent CJK characters. The narrow, unambiguous shape of
#: text corruption — and cheap enough to run on every scene.
_REPEATED_CJK_RE = re.compile(r'([\u4e00-\u9fff])\1')
_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
_ENTITY_RE = re.compile(r'&[a-zA-Z#0-9]+;')
_SCRIPT_STYLE_RE = re.compile(r'(?is)<(script|style)\b.*?</\1>')


def visible_text(html: str) -> list[str]:
    """The text strings a VIEWER would read, in document order.

    Script and style bodies are removed first: their contents never reach the
    frame, and judging them would flag identifiers and CSS as prose.
    """
    body = _SCRIPT_STYLE_RE.sub(' ', html or '')
    out: list[str] = []
    for chunk in re.findall(r'>([^<>]+)<', body):
        text = _ENTITY_RE.sub(' ', chunk).strip()
        if text:
            out.append(text)
    return out


def check_text_fidelity(html: str, scene: dict) -> list[str]:
    """Reject a composition whose on-frame text CORRUPTS its source beat.

    The author is handed the beat's ``text`` / ``on_screen`` / ``visual`` and
    may legitimately condense or re-word them — a headline is supposed to be
    a rewrite, not a quotation. So this gate judges only what no rewrite can
    excuse: a **doubled CJK character that does not occur doubled anywhere in
    the source**.

    **Why the source cross-check is load-bearing, with numbers.** Measured
    over the 41 authored compositions on disk (166 visible strings), the bare
    "repeated adjacent CJK" pattern fires **14** times — but 13 of those are
    ordinary reduplicated Chinese words that the beat itself contains
    (恰恰 / 源源 / 准准 / 证证 / 偷偷). Requiring the pair to be ABSENT from the
    source drops it to **exactly 1 hit: the real 极极 defect**. Without the
    cross-check this gate would degrade 13 good scenes to plain template
    cards — worse than the bug it fixes.

    **Deliberately NOT implemented: "on-frame text must appear in the
    source".** Measured on the same corpus, verbatim containment flags
    **59%** of CJK strings, and narrowing it to "introduces no character
    absent from the source" still flags **12%** — including 「核聚变商业化浪潮」
    and 「全球资本竞逐新高地」, which are exactly the well-written headlines this
    whole effort is trying to produce. A gate that punishes good copywriting
    is a wrong gate, so that rule is left out until a formulation exists that
    measures clean on real output.

    Returns human-readable errors (empty = pass), same contract as the
    sibling gates so findings flow into the repair prompt unchanged.
    """
    if not html:
        return []
    source = ''.join(str(scene.get(k) or '')
                     for k in ('text', 'on_screen', 'visual')) if scene else ''
    errors: list[str] = []
    for string in visible_text(html):
        if not _CJK_RE.search(string):
            continue
        for match in _REPEATED_CJK_RE.finditer(string):
            pair = match.group(0)
            if pair in source:
                continue  # a real reduplicated word — the beat says so
            context = string[max(0, match.start() - 8):match.start() + 10]
            errors.append(
                f'on-frame text corrupts the source: {pair!r} is doubled in '
                f'"{context}" but appears nowhere doubled in this scene\'s '
                f'narration/caption/art-direction — fix the typo')
            break  # one finding per string is enough to send it back
    return errors


# ── Media probing ─────────────────────────────────────────

def _probe_with_ffprobe(ffprobe: str, path: str) -> dict | None:
    try:
        out = subprocess.run(
            [ffprobe, '-v', 'error',
             '-show_entries', 'stream=codec_type,codec_name,width,height,r_frame_rate,duration',
             '-of', 'json', path],
            capture_output=True, text=True, timeout=60)
    except Exception as e:
        logger.warning('[MotionVideo] ffprobe failed for %s: %s', path, e)
        return None
    if out.returncode != 0:
        logger.warning('[MotionVideo] ffprobe rc=%s for %s: %.300s',
                       out.returncode, path, out.stderr)
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        logger.warning('[MotionVideo] ffprobe JSON parse failed for %s: %s', path, e)
        return None
    info: dict = {'has_audio': False}
    for st in data.get('streams', []):
        if st.get('codec_type') == 'video':
            info['codec'] = st.get('codec_name', '')
            info['width'] = int(st.get('width') or 0)
            info['height'] = int(st.get('height') or 0)
            rate = st.get('r_frame_rate', '0/1')
            try:
                num, den = rate.split('/')
                info['fps'] = round(float(num) / float(den), 3) if float(den) else 0.0
            except (ValueError, ZeroDivisionError) as _e:
                logger.debug('probe with ffprobe: unparseable/zero divisor (%s)', _e)
                info['fps'] = 0.0
            if st.get('duration'):
                try:
                    info['duration'] = float(st['duration'])
                except (TypeError, ValueError) as e:
                    # Dropped duration surfaces downstream only as a generic
                    # 'duration 0 != expected' — log the root cause here.
                    logger.debug('[MotionVideo] ffprobe duration parse failed for %s (%r): %s', path, st['duration'], e)
        elif st.get('codec_type') == 'audio':
            info['has_audio'] = True
    return info if 'codec' in info else None


_FFMPEG_I_VIDEO_RE = re.compile(
    r'Stream .*Video: (\w+).*?(\d{2,5})x(\d{2,5}).*?([0-9.]+) fps')
_FFMPEG_I_AUDIO_RE = re.compile(r'Stream .*Audio:')
_FFMPEG_I_DUR_RE = re.compile(r'Duration: (\d+):(\d+):(\d+\.\d+)')


def _probe_with_ffmpeg(ffmpeg: str, path: str) -> dict | None:
    """Fallback probe parsing ``ffmpeg -i`` stderr (ffprobe absent)."""
    try:
        out = subprocess.run([ffmpeg, '-hide_banner', '-i', path],
                             capture_output=True, text=True, timeout=60)
    except Exception as e:
        logger.warning('[MotionVideo] ffmpeg -i failed for %s: %s', path, e)
        return None
    blob = out.stderr + out.stdout
    v = _FFMPEG_I_VIDEO_RE.search(blob)
    if not v:
        return None
    info: dict = {'codec': v.group(1), 'width': int(v.group(2)),
                  'height': int(v.group(3)), 'fps': float(v.group(4)),
                  'has_audio': bool(_FFMPEG_I_AUDIO_RE.search(blob))}
    d = _FFMPEG_I_DUR_RE.search(blob)
    if d:
        info['duration'] = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))
    return info


def probe_video(path: str, *, ffprobe: str = '', ffmpeg: str = '') -> dict | None:
    """Probe a media file → ``{codec,width,height,fps,duration,has_audio}``.

    Uses ffprobe when resolvable, else falls back to parsing ``ffmpeg -i``.
    Returns None on failure (logged).
    """
    if not os.path.isfile(path):
        logger.warning('[MotionVideo] probe_video: not a file: %s', path)
        return None
    if not ffprobe or not ffmpeg:
        from lib.motion_video._env import ffmpeg_bin, ffprobe_bin
        ffprobe = ffprobe or ffprobe_bin()
        ffmpeg = ffmpeg or ffmpeg_bin()
    if ffprobe:
        info = _probe_with_ffprobe(ffprobe, path)
        if info is not None:
            return info
    if ffmpeg:
        return _probe_with_ffmpeg(ffmpeg, path)
    logger.warning('[MotionVideo] probe_video: neither ffprobe nor ffmpeg available')
    return None


def verify_spec(probe: dict, *, width: int, height: int, fps: float,
                duration: float, tol: float = 0.15,
                require_silent: bool = True) -> list[str]:
    """Verify a :func:`probe_video` result against the expected scene spec."""
    errors: list[str] = []
    if not probe:
        return ['probe failed (no media info)']
    if probe.get('width') != width or probe.get('height') != height:
        errors.append(f'resolution {probe.get("width")}x{probe.get("height")} '
                      f'!= expected {width}x{height}')
    got_fps = float(probe.get('fps') or 0)
    if abs(got_fps - fps) > 0.6:  # 29.97 vs 30 style slack
        errors.append(f'fps {got_fps} != expected {fps}')
    got_dur = float(probe.get('duration') or 0)
    if abs(got_dur - duration) > tol:
        errors.append(f'duration {got_dur:.3f}s != expected {duration:.3f}s '
                      f'(tol {tol}s)')
    if require_silent and probe.get('has_audio'):
        errors.append('scene MP4 has an audio track (scenes must be silent; '
                      'narration is muxed at concat time)')
    return errors
