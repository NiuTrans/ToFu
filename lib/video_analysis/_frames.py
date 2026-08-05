"""lib/video_analysis/_frames.py — video → timestamped JPEG frames.

Strategy (research-backed, 2026-08-04 survey): a UNIFORM base layer gives
guaranteed temporal coverage, and a SCENE-CUT layer (ffmpeg ``select=gt(scene,…)``)
adds the moments uniform sampling misses — quick cuts, slide changes. The two
are merged, deduped (±1s), sorted by time and capped at the duration-tier target
(≤60s→16 / ≤600s→32 / else 64).

Frames are scaled to ≤1568px long side and JPEG-encoded AT EXTRACTION (the
``_CLAUDE_IMAGE_MAX_PX`` rationale: born cache-stable, never re-encoded later),
then persisted into the SAME ``uploads/images`` store regular uploads use —
so they ride the existing ``/api/images/`` serving + ``_validate_image_blocks``
disk-resolution path unchanged, and survive conversation reload / resume /
multi-turn exactly like a user-uploaded image.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

from lib.log import get_logger

from lib.video_analysis._config import (
    FRAME_JPEG_Q,
    FRAME_LONG_SIDE_PX,
    frame_target_for_duration,
    scene_score_threshold,
)

logger = get_logger(__name__)

_PTS_TIME_RE = re.compile(r'pts_time:([0-9.]+)')

#: scale filter: cap BOTH dimensions at the long-side cap, keep aspect, even dims.
_SCALE_FILTER = (
    f"scale=min({FRAME_LONG_SIDE_PX}\\,iw):min({FRAME_LONG_SIDE_PX}\\,ih):"
    'force_original_aspect_ratio=decrease:force_divisible_by=2'
)


def _ffmpeg() -> str:
    from lib.motion_video._env import ensure_ffmpeg
    return ensure_ffmpeg(install=True)


def _run(cmd: list[str], timeout: float, what: str) -> subprocess.CompletedProcess:
    logger.debug('[VideoFrames] %s: %s', what, ' '.join(cmd[:6]) + ' …')
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f'{what} timed out after {timeout:.0f}s') from e
    if proc.returncode != 0:
        raise RuntimeError(f'{what} failed rc={proc.returncode}: {(proc.stderr or "")[:400]}')
    return proc


def _scene_cut_times(ffmpeg: str, video_path: str, duration_s: float) -> list[float]:
    """One decode-only pass collecting scene-cut timestamps via showinfo."""
    thr = scene_score_threshold()
    if thr <= 0:
        return []
    cmd = [
        ffmpeg, '-hide_banner', '-i', video_path,
        '-vf', f'select=gt(scene\\,{thr}),showinfo',
        '-vsync', 'vfr', '-f', 'null', '-',
    ]
    try:
        proc = _run(cmd, timeout=max(180.0, duration_s * 2), what='scene scan')
    except RuntimeError as e:
        # Scene detection is an enhancement — never fail the video over it.
        logger.warning('[VideoFrames] scene scan failed, uniform-only: %s', e)
        return []
    times = [float(m.group(1)) for m in _PTS_TIME_RE.finditer(proc.stderr or '')]
    logger.info('[VideoFrames] scene scan: %d cut(s) (thr=%.2f)', len(times), thr)
    return times


def _extract_uniform(ffmpeg: str, video_path: str, duration_s: float,
                     count: int, out_dir: str) -> list[dict]:
    """Extract ``count`` evenly-spaced frames; timestamps are computed (the fps
    filter picks frames at exact 1/fps intervals, so frame n sits at
    ``(n + 0.5) * duration / actual_count`` — midpoint of its sampling cell)."""
    fps = count / max(duration_s, 0.1)
    pattern = os.path.join(out_dir, 'u_%04d.jpg')
    _run([
        ffmpeg, '-hide_banner', '-i', video_path,
        '-vf', f'fps={fps:.6f},{_SCALE_FILTER}',
        '-q:v', str(FRAME_JPEG_Q), '-vsync', 'vfr', pattern,
    ], timeout=max(180.0, duration_s * 2), what='uniform extraction')
    files = sorted(f for f in os.listdir(out_dir) if f.startswith('u_'))
    n = len(files)
    frames = []
    for i, fname in enumerate(files):
        t = min(duration_s, (i + 0.5) * duration_s / max(n, 1))
        frames.append({'path': os.path.join(out_dir, fname), 't': round(t, 2)})
    return frames


def _extract_single(ffmpeg: str, video_path: str, t: float, out_dir: str,
                    tag: str) -> dict | None:
    out = os.path.join(out_dir, f's_{tag}.jpg')
    try:
        _run([
            ffmpeg, '-hide_banner', '-ss', f'{t:.3f}', '-i', video_path,
            '-frames:v', '1', '-vf', _SCALE_FILTER,
            '-q:v', str(FRAME_JPEG_Q), '-y', out,
        ], timeout=60.0, what=f'scene frame @{t:.1f}s')
    except RuntimeError as e:
        logger.warning('[VideoFrames] scene frame @%.1fs skipped: %s', t, e)
        return None
    return {'path': out, 't': round(t, 2)} if os.path.isfile(out) else None


def _fmt_video_ts(seconds: float) -> str:
    """Format a frame timestamp as MM:SS (H:MM:SS past the hour)."""
    s = max(0, int(seconds))
    if s >= 3600:
        return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}'
    return f'{s // 60:02d}:{s % 60:02d}'


def _thin_frames(frames: list, n: int) -> list:
    """Uniformly thin ``frames`` to at most ``n`` entries, keeping the first
    and last — temporal coverage beats recency for video understanding."""
    if n <= 0:
        return []
    if len(frames) <= n:
        return frames
    if n == 1:
        return frames[:1]
    idx = sorted({round(i * (len(frames) - 1) / (n - 1)) for i in range(n)})
    return [frames[i] for i in idx]


def _merge_scene_extras(uniform_ts: list[float], cut_times: list[float],
                        budget: int) -> list[float]:
    """Pick scene-cut times that ADD information over the uniform layer.

    A cut within ±1s of an already-sampled time is redundant; cuts dedupe
    against each other the same way. Note the consequence: when the uniform
    spacing is under ~2s (short clips), EVERY cut is within 1s of a uniform
    frame and the scene layer is intentionally empty — it only earns its
    keep on longer videos where uniform spacing leaves gaps.
    """
    picked: list[float] = []
    for t in cut_times:
        if len(picked) >= budget:
            break
        if any(abs(t - u) < 1.0 for u in uniform_ts):
            continue
        if any(abs(t - p) < 1.0 for p in picked):
            continue
        picked.append(t)
    return picked


def extract_frames(video_path: str, duration_s: float, scratch_dir: str) -> list[dict]:
    """Extract merged uniform+scene frames → ``[{path, t}]`` sorted by time.

    Layout of the target budget: 2/3 uniform base + up to 1/3 scene-cut extras
    (deduped ±1s against the uniform layer). Total never exceeds the
    duration-tier target, which never exceeds ``FRAME_CEILING``.
    """
    target = frame_target_for_duration(duration_s)
    uniform_count = max(4, target - target // 3)
    scene_budget = target - uniform_count

    ffmpeg = _ffmpeg()
    if not ffmpeg:
        raise RuntimeError('ffmpeg unavailable (imageio-ffmpeg install failed)')

    frames = _extract_uniform(ffmpeg, video_path, duration_s, uniform_count, scratch_dir)
    logger.info('[VideoFrames] uniform layer: %d/%d frames (duration=%.1fs)',
                len(frames), uniform_count, duration_s)

    if scene_budget > 0:
        picked = _merge_scene_extras(
            [f['t'] for f in frames],
            _scene_cut_times(ffmpeg, video_path, duration_s),
            scene_budget)
        for i, t in enumerate(picked):
            one = _extract_single(ffmpeg, video_path, t, scratch_dir, f'{i:03d}')
            if one:
                frames.append(one)
        if picked:
            logger.info('[VideoFrames] scene layer: +%d frame(s)', len(picked))

    frames.sort(key=lambda f: f['t'])
    return frames[:target]


def persist_frames(frames: list[dict], images_dir: str) -> list[dict]:
    """Move extracted JPEGs into the uploads images store → durable URLs.

    Returns ``[{url, t, bytes}]`` in time order. A frame that fails to persist
    is dropped (logged) rather than failing the whole video.
    """
    out: list[dict] = []
    for i, fr in enumerate(frames):
        try:
            with open(fr['path'], 'rb') as f:
                data = f.read()
        except Exception as e:
            logger.warning('[VideoFrames] frame read failed (%s): %s', fr['path'], e)
            continue
        filename = f"{int(time.time() * 1000)}_{os.urandom(4).hex()}_vf{i:03d}.jpg"
        dest = os.path.join(images_dir, filename)
        try:
            with open(dest, 'wb') as f:
                f.write(data)
        except Exception as e:
            logger.error('[VideoFrames] frame persist failed (%s): %s', dest, e)
            continue
        out.append({'url': f'/api/images/{filename}', 't': fr['t'], 'bytes': len(data)})
    logger.info('[VideoFrames] persisted %d/%d frames to %s',
                len(out), len(frames), images_dir)
    return out
