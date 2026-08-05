"""lib/motion_video/_concat.py — Scene MP4 normalization + concatenation.

Final assembly of the motion-video pipeline: take the per-scene silent MP4s
and produce ``final.mp4``. Two paths, mirroring auto-motion's rule
("confirm uniform specs first; transcode-normalize when they differ"):

  * **uniform specs** (same codec/size/fps, no audio) → concat demuxer with
    ``-c copy`` (fast + lossless);
  * **mismatched specs** → single-pass re-encode to the first input's spec
    (scale + fps + yuv420p + silent).

The output is written atomically (tmp file + ``os.replace``) and the result
is probe-verified: total duration ≈ Σ scene durations (±0.5s), no audio
track.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['concat_mp4s', 'burn_in_subtitles']


def _uniform_spec(probes: list[dict]) -> bool:
    """True when every probe shares codec/size/fps and carries no audio."""
    if not probes or any(not p for p in probes):
        return False
    first = probes[0]
    for p in probes:
        if p.get('has_audio'):
            return False
        if (p.get('codec') != first.get('codec')
                or p.get('width') != first.get('width')
                or p.get('height') != first.get('height')
                or abs(float(p.get('fps') or 0) - float(first.get('fps') or 0)) > 0.6):
            return False
    return True


def _run_ffmpeg(args: list[str], *, timeout: int, abort_event=None,
                env: dict | None = None) -> dict:
    from lib.motion_video._env import build_render_env
    env = env or build_render_env()
    start = time.time()
    try:
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except FileNotFoundError as _e:
        logger.debug('run ffmpeg: missing (%s)', _e)
        return {'rc': None, 'err': f'ffmpeg not found: {args[0]}',
                'elapsed': 0.0, 'category': 'env_missing'}
    except Exception as e:
        logger.error('[MotionVideo] ffmpeg spawn failed: %s', e, exc_info=True)
        return {'rc': None, 'err': str(e), 'elapsed': 0.0, 'category': 'io'}

    out_chunks: list[str] = []
    category = ''
    deadline = start + timeout if timeout and timeout > 0 else None
    while True:
        try:
            _out, err = proc.communicate(timeout=1.0)
            out_chunks.append(err or '')
            break
        except subprocess.TimeoutExpired:
            if abort_event is not None and abort_event.is_set():
                category = 'aborted'
                break
            if deadline is not None and time.time() > deadline:
                category = 'timeout'
                break
    if category:
        import signal as _sig
        try:
            os.killpg(proc.pid, _sig.SIGKILL)
        except Exception as e:
            logger.debug('[MotionVideo] killpg failed: %s', e)
        try:
            proc.communicate(timeout=10)
        except Exception as e:
            logger.debug('[MotionVideo] communicate-after-kill failed: %s', e)
    return {'rc': proc.poll(), 'err': ''.join(out_chunks),
            'elapsed': time.time() - start, 'category': category}


def concat_mp4s(inputs: list[str], output: str, *, timeout: int = 1800,
                abort_event=None) -> dict:
    """Concatenate scene MP4s into ``final.mp4`` (atomic + probe-verified).

    Args:
        inputs: ordered scene MP4 paths (≥1).
        output: final MP4 path.
        timeout: wall-clock seconds for each ffmpeg invocation.
        abort_event: optional threading.Event for cooperative cancel.

    Returns a result dict; on success ``{'ok': True, 'output', 'duration',
    'mode', 'elapsed'}`` where mode is ``'copy'`` or ``'reencode'``.
    """
    from lib.motion_video._env import ffmpeg_bin
    from lib.motion_video._gates import probe_video

    if not inputs:
        return {'ok': False, 'category': 'io', 'detail': 'no input scenes'}
    for p in inputs:
        if not os.path.isfile(p):
            return {'ok': False, 'category': 'io',
                    'detail': f'missing scene file: {p}'}
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return {'ok': False, 'category': 'env_missing',
                'detail': 'ffmpeg not found (pip install imageio-ffmpeg)'}

    probes = [probe_video(p) for p in inputs]
    if any(p is None for p in probes):
        bad = inputs[probes.index(None)]
        return {'ok': False, 'category': 'io',
                'detail': f'cannot probe scene file: {bad}'}
    expected_total = sum(float(p.get('duration') or 0) for p in probes)

    out_dir = os.path.dirname(os.path.abspath(output)) or '.'
    os.makedirs(out_dir, exist_ok=True)
    uniform = _uniform_spec(probes)
    mode = 'copy' if uniform else 'reencode'
    logger.info('[MotionVideo] concat %d scene(s) → %s (mode=%s, Σ=%.2fs)',
                len(inputs), output, mode, expected_total)

    list_fd, list_path = tempfile.mkstemp(prefix='mvconcat-', suffix='.txt',
                                          dir=out_dir, text=True)
    tmp_out = output + '.tmp.mp4'
    try:
        with os.fdopen(list_fd, 'w') as lf:
            for p in inputs:
                safe = os.path.abspath(p).replace("'", "'\\''")
                lf.write(f"file '{safe}'\n")
        args = [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', list_path]
        if uniform:
            args += ['-c', 'copy']
        else:
            first = probes[0]
            args += [
                '-vf', f"scale={first['width']}:{first['height']},setsar=1",
                '-r', str(int(round(float(first.get('fps') or 30)))),
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18',
                '-preset', 'medium', '-an',
            ]
        args += ['-movflags', '+faststart', tmp_out]
        res = _run_ffmpeg(args, timeout=timeout, abort_event=abort_event)
        if res['category'] or res['rc'] != 0:
            return {'ok': False,
                    'category': res['category'] or 'unknown',
                    'detail': res['err'][-1500:]}
        if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
            return {'ok': False, 'category': 'io',
                    'detail': 'ffmpeg produced no output'}
        os.replace(tmp_out, output)
    finally:
        try:
            os.unlink(list_path)
        except OSError as e:
            logger.debug('[MotionVideo] concat list cleanup failed: %s', e)
        if os.path.isfile(tmp_out):
            try:
                os.unlink(tmp_out)
            except OSError as e:
                logger.debug('[MotionVideo] tmp output cleanup failed: %s', e)

    # ── Post-verify: duration ≈ Σ scenes, silent ──
    final_probe = probe_video(output)
    detail = ''
    if final_probe is None:
        return {'ok': False, 'category': 'io',
                'detail': 'post-concat probe failed'}
    got = float(final_probe.get('duration') or 0)
    if expected_total > 0 and abs(got - expected_total) > 0.5:
        detail = (f'duration mismatch: final {got:.3f}s vs Σ scenes '
                  f'{expected_total:.3f}s')
        logger.warning('[MotionVideo] concat verify: %s', detail)
        return {'ok': False, 'category': 'io', 'detail': detail}
    if final_probe.get('has_audio'):
        logger.warning('[MotionVideo] concat verify: final has an audio track')
        return {'ok': False, 'category': 'io',
                'detail': 'final MP4 unexpectedly has an audio track'}

    logger.info('[MotionVideo] concat done: %s (%.2fs)', output, got)
    return {'ok': True, 'output': output, 'duration': round(got, 3),
            'mode': mode, 'elapsed': round(res['elapsed'], 2)}


def _escape_filter_path(path: str) -> str:
    """Escape a filesystem path for use inside an ffmpeg filtergraph."""
    return (path.replace('\\', '\\\\')
                .replace(':', '\\:')
                .replace("'", "\\'"))


def _font_burn_failed(stderr: str) -> bool:
    """True when libass told us (stderr only) it could not draw the text.

    libass NEVER fails the ffmpeg run on font problems — the process exits
    0 with a perfectly valid video that simply has no subtitles rendered
    (2026-07-26: broken fontconfig → ``failed to find any fallback with
    glyph 0x0``; font found but missing CJK coverage → ``... glyph
    0x6D4B``). The only honest signal is this log line, so we promote it
    to a real failure category instead of shipping a silent no-op burn.
    """
    return 'failed to find any fallback' in (stderr or '')


def _frame_png(video: str, t: float, dest: str, *, ffmpeg: str,
               env: dict) -> bool:
    """Extract the frame at ``t`` seconds into ``dest``. False on failure."""
    try:
        proc = subprocess.run(
            [ffmpeg, '-y', '-v', 'error', '-ss', f'{max(0.0, t):.3f}',
             '-i', video, '-frames:v', '1', dest],
            env=env, capture_output=True, text=True, timeout=120)
    except Exception as e:
        logger.debug('[MotionVideo] frame extract failed: %s', e)
        return False
    return proc.returncode == 0 and os.path.isfile(dest) \
        and os.path.getsize(dest) > 0


def _subtitle_ink_bbox(before_png: str, after_png: str):
    """BBox of pixels the burn CHANGED, i.e. the subtitle ink itself.

    Differencing against the pre-burn frame is what makes this work over a
    real composition: the scene's own artwork is identical in both frames, so
    whatever moved is the subtitle. Scanning for "bright pixels" instead would
    just find the scene's own white headline.

    Returns ``(x0, y0, x1, y1)``, None when nothing changed (a no-op burn), or
    ``'unavailable'`` when numpy/PIL are missing so the caller can tell
    "cannot check" apart from "check passed".
    """
    try:
        import numpy as np
        from PIL import Image
    except Exception as e:
        logger.debug('[MotionVideo] ink scan needs numpy+PIL: %s', e)
        return 'unavailable'
    try:
        a = np.asarray(Image.open(before_png).convert('L'), dtype=np.int16)
        b = np.asarray(Image.open(after_png).convert('L'), dtype=np.int16)
    except Exception as e:
        logger.warning('[MotionVideo] ink scan could not read frames: %s', e)
        return 'unavailable'
    if a.shape != b.shape:
        return 'unavailable'
    # 24/255 ignores encoder noise while still catching antialiased outline.
    ys, xs = np.where(np.abs(b - a) > 24)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _verify_safe_box(video_path: str, burned: str, cues, width: int,
                     height: int, *, ffmpeg: str, env: dict,
                     workdir: str) -> dict:
    """Assert burned ink lands inside the safe box, by REAL pixel inspection.

    This closes the gate hole that let the overflow ship: the only burn check
    was :func:`_font_burn_failed`, which detects "no glyph was drawn at all"
    and is completely blind to "drawn, but running off both edges" (measured
    2026-07-28: ink bbox ``x[0..1079]`` on a 1080px frame passed every gate).

    Returns ``{'ok': True, 'checked': n}``, or an ``ok=False`` dict with
    category ``subtitle_overflow`` / ``subtitle_missing``. Inability to
    inspect (no numpy/PIL, extraction failure) is NOT a failure — it reports
    ``checked=0`` so a thin environment degrades instead of blocking.
    """
    from lib.motion_video._subtitle import safe_box, style_for_frame, wrap_line

    if not cues or width <= 0 or height <= 0:
        return {'ok': True, 'checked': 0}
    left, right = safe_box(width, height)
    style = style_for_frame(width, height)
    # Check the cues most likely to overflow: widest wrapped line first.
    ranked = sorted(
        cues,
        key=lambda c: -max((len(ln) for ln in wrap_line(c[2], style)),
                           default=0))
    checked = 0
    for start, end, text in ranked[:3]:
        mid = float(start) + (float(end) - float(start)) / 2.0
        b_png = os.path.join(workdir, f'_sbcheck_before_{checked}.png')
        a_png = os.path.join(workdir, f'_sbcheck_after_{checked}.png')
        try:
            if not _frame_png(video_path, mid, b_png, ffmpeg=ffmpeg, env=env):
                continue
            if not _frame_png(burned, mid, a_png, ffmpeg=ffmpeg, env=env):
                continue
            box = _subtitle_ink_bbox(b_png, a_png)
            if box == 'unavailable':
                return {'ok': True, 'checked': checked}
            if box is None:
                return {'ok': False, 'category': 'subtitle_missing',
                        'detail': (f'the burn changed no pixels at t={mid:.2f}s '
                                   f'though cue text was present — subtitles '
                                   f'did not render')}
            x0, _y0, x1, _y1 = box
            if x0 < left or x1 > right:
                return {
                    'ok': False, 'category': 'subtitle_overflow',
                    'detail': (
                        f'burned subtitle ink spans x[{x0}..{x1}] at '
                        f't={mid:.2f}s but the safe box for a {width}x{height} '
                        f'frame is x[{left}..{right}] — the line runs past the '
                        f'frame edge and is clipped. Text was '
                        f'{len(text)} chars: {text[:40]!r}')}
            checked += 1
        finally:
            for p in (b_png, a_png):
                try:
                    if os.path.isfile(p):
                        os.unlink(p)
                except OSError as e:
                    logger.debug('[MotionVideo] safe-box temp cleanup: %s', e)
    return {'ok': True, 'checked': checked}


def burn_in_subtitles(video_path: str, srt_path: str, output: str, *,
                      fontsdir: str = '', force_style: str = '',
                      timeout: int = 1800, abort_event=None,
                      verify_safe_box: bool = True) -> dict:
    """Burn a sidecar SRT into the video (hard subtitles), atomic + verified.

    Re-encodes (libx264) — there is no lossless way to hard-sub.

    **The SRT is converted to a geometry-bound ASS document first**
    (:mod:`lib.motion_video._subtitle`), because a bare SRT is exactly what
    produced the shipped overflow: with no ``[Script Info]`` header libass
    assumes a 384x288 reference frame and scales its default style by
    ``height/288`` (a 5x blow-up at 1440px), and it will not wrap CJK at all,
    so an unspaced Chinese sentence renders as one line off both edges.
    ``force_style`` cannot fix either problem — it only reaches
    ``[V4+ Styles]``, never ``PlayResX``/``PlayResY`` (measured, along with
    the ``original_size`` option, which also had no effect).

    Frame geometry is read from the VIDEO ITSELF rather than taken from a
    caller argument, so a new call site cannot forget to pass it and silently
    reintroduce the 384x288 default.

    ``fontsdir`` adds a font search dir; ``force_style`` still appends for
    operator overrides. Post-verified: output exists, duration is preserved
    (±0.5s), and — when ``verify_safe_box`` — the burned ink really lands
    inside the frame's safe box (a real pixel diff, not a log scan).
    """
    from lib.motion_video._env import build_render_env, ffmpeg_bin
    from lib.motion_video._gates import probe_video

    for p, label in ((video_path, 'video'), (srt_path, 'srt')):
        if not os.path.isfile(p):
            return {'ok': False, 'category': 'io',
                    'detail': f'missing {label} file: {p}'}
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return {'ok': False, 'category': 'env_missing',
                'detail': 'ffmpeg not found (pip install imageio-ffmpeg)'}

    # Geometry comes from the real pixels — the authoritative source.
    src_probe = probe_video(video_path)
    frame_w = int((src_probe or {}).get('width') or 0)
    frame_h = int((src_probe or {}).get('height') or 0)

    sub_path = os.path.abspath(srt_path)
    cues: list = []
    if frame_w > 0 and frame_h > 0:
        from lib.motion_video._srt import parse_srt
        from lib.motion_video._subtitle import build_ass
        try:
            with open(srt_path, encoding='utf-8') as f:
                cues = [(e.start, e.end, e.text) for e in parse_srt(f.read())]
        except OSError as e:
            logger.debug('[MotionVideo] cannot read srt %s: %s', srt_path, e)
            return {'ok': False, 'category': 'io',
                    'detail': f'cannot read srt: {e}'}
        if cues:
            ass_text, warns = build_ass(cues, frame_w, frame_h)
            ass_path = os.path.join(
                os.path.dirname(os.path.abspath(output)) or '.',
                os.path.basename(output) + '.burn.ass')
            try:
                with open(ass_path, 'w', encoding='utf-8') as f:
                    f.write(ass_text)
                sub_path = ass_path
            except OSError as e:
                logger.warning('[MotionVideo] cannot stage ASS (%s) — '
                               'falling back to the raw SRT', e)
            if warns:
                logger.warning('[MotionVideo] %d over-long cue(s) in this burn',
                               len(warns))
    else:
        logger.warning('[MotionVideo] could not probe %s for frame size — '
                       'burning the raw SRT without a geometry contract',
                       video_path)

    filt = f"subtitles='{_escape_filter_path(sub_path)}'"
    if fontsdir:
        filt += f":fontsdir='{_escape_filter_path(os.path.abspath(fontsdir))}'"
    if force_style:
        filt += f":force_style='{force_style}'"

    tmp_out = output + '.tmp.mp4'
    args = [ffmpeg, '-y', '-i', video_path, '-vf', filt,
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18',
            '-preset', 'medium', '-an', '-movflags', '+faststart', tmp_out]
    logger.info('[MotionVideo] burn-in %s → %s', srt_path, output)
    res = _run_ffmpeg(args, timeout=timeout, abort_event=abort_event)
    if res['category'] or res['rc'] != 0:
        return {'ok': False, 'category': res['category'] or 'unknown',
                'detail': res['err'][-1500:]}
    if _font_burn_failed(res['err']):
        logger.warning('[MotionVideo] burn-in refused: libass resolved no '
                       'font for the subtitle glyphs — the burn would be a '
                       'silent no-op')
        try:
            os.unlink(tmp_out)
        except OSError as _e:
            logger.debug('burn in subtitles: unreadable (%s)', _e)
            pass  # tmp output may not exist yet — nothing to clean
        return {'ok': False, 'category': 'font_missing',
                'detail': 'libass resolved no usable font for the subtitle '
                          'text (fontconfig config missing, or no installed '
                          'font covers the glyphs — install a CJK-capable '
                          'font or pass burn_in_fontsdir); refusing to ship '
                          'a silent no-op burn'}
    if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
        return {'ok': False, 'category': 'io',
                'detail': 'ffmpeg produced no output'}

    # ── Safe-box verification BEFORE the output is promoted ──
    # Runs on tmp_out so a defective burn never reaches the deliverable path.
    safe_checked = 0
    if verify_safe_box and cues and frame_w > 0 and frame_h > 0:
        vres = _verify_safe_box(
            video_path, tmp_out, cues, frame_w, frame_h, ffmpeg=ffmpeg,
            env=build_render_env(),
            workdir=os.path.dirname(os.path.abspath(output)) or '.')
        if not vres.get('ok'):
            logger.warning('[MotionVideo] burn-in rejected: %s',
                           vres.get('detail'))
            try:
                os.unlink(tmp_out)
            except OSError as e:
                logger.debug('[MotionVideo] rejected burn cleanup: %s', e)
            return vres
        safe_checked = vres.get('checked', 0)

    os.replace(tmp_out, output)

    v_probe = probe_video(video_path)
    f_probe = probe_video(output)
    if f_probe is None:
        return {'ok': False, 'category': 'io',
                'detail': 'post-burn probe failed'}
    if v_probe:
        dv = abs(float(f_probe.get('duration') or 0)
                 - float(v_probe.get('duration') or 0))
        if dv > 0.5:
            return {'ok': False, 'category': 'io',
                    'detail': f'burned duration drifted {dv:.3f}s'}
    logger.info('[MotionVideo] burn-in done: %s (safe-box verified on %d cue(s))',
                output, safe_checked)
    return {'ok': True, 'output': output,
            'duration': round(float(f_probe.get('duration') or 0), 3),
            'safe_box_checked': safe_checked,
            'elapsed': round(res['elapsed'], 2)}
