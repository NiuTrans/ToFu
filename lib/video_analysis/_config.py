"""lib/video_analysis/_config.py — env-driven caps and tunables for video ingest.

Single home for every limit the video-upload pipeline enforces, so routes,
the frame extractor and tests all read the SAME numbers:

  * size / duration caps (owner decision 2026-08-04: 512 MiB, 15 min for P1)
  * duration→frame-count tiers (≤60s → 16, ≤600s → 32, else 64 ceiling)
  * frame geometry (1568px long side — the SAME uniform cap rationale as
    ``lib.llm.body._images._CLAUDE_IMAGE_MAX_PX``: the Claude vision tower
    downscales to ~1568px internally, so a fixed cap is shrunk at most once
    and never re-encoded across rounds → no prompt-cache churn)
  * local-disk scratch root (frames/artifacts must NOT be decoded on the
    FUSE mount — a bad FUSE window stalls ffmpeg mid-decode; measured 17min
    stalls in the 2026-08 journal)
"""

from __future__ import annotations

import os
import tempfile
import time

from lib.log import get_logger

logger = get_logger(__name__)

# Accepted upload containers (by EXTENSION; magic bytes are sniffed separately).
VIDEO_EXTS: frozenset[str] = frozenset({
    '.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi',
})

#: Hard ceiling on extracted frames for ANY video — the wire/token budget
#: math in ``lib.model_info.video_frame_budget`` assumes this ceiling.
FRAME_CEILING = 64

#: Long-side pixel cap applied AT EXTRACTION (ffmpeg scale filter), so frames
#: are born cache-stable and never need a retroactive re-encode.
FRAME_LONG_SIDE_PX = 1568

#: ffmpeg -q:v for frame JPEGs (≈ quality 80 — text-on-screen stays readable,
#: a 1568px frame lands ~100-200KB).
FRAME_JPEG_Q = 4

#: Transcript longer than this is truncated with a marker (keeps the message
#: payload bounded even for a 15-min dense talk track).
TRANSCRIPT_CHAR_CAP = 30_000

#: Registry entries older than this are pruned lazily on the next create
#: (frames/messages persist independently — the registry is only live status).
RECORD_TTL_S = 7 * 86400

# One directory scan never walks an attacker- or crash-amplified temp tree
# without a bound. Repeated uploads/startups make forward progress if more
# entries exist.
SCRATCH_RECLAIM_SCAN_CAP = 512


def video_analysis_enabled() -> bool:
    """Kill switch: ``TOFU_VIDEO_ANALYSIS=0`` disables the feature entirely."""
    return os.environ.get('TOFU_VIDEO_ANALYSIS', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def video_max_bytes() -> int:
    """Max accepted video upload size. Default 512 MiB (owner 2026-08-04)."""
    try:
        v = int(os.environ.get('TOFU_VIDEO_MAX_BYTES', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[Video] bad TOFU_VIDEO_MAX_BYTES, using default: %s', e)
    return 512 * 1024 * 1024


def video_max_duration_s() -> float:
    """Max accepted video duration in seconds. Default 900s (15 min, P1)."""
    try:
        v = float(os.environ.get('TOFU_VIDEO_MAX_DURATION_S', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[Video] bad TOFU_VIDEO_MAX_DURATION_S, using default: %s', e)
    return 900.0


def video_scratch_ttl_s() -> float:
    """TTL for reconstructible local decode state left by a killed process."""
    minimum = max(3600.0, video_max_duration_s() * 2 + 600.0)
    try:
        configured = float(
            os.environ.get('TOFU_VIDEO_SCRATCH_TTL_S', '') or 0)
        if configured > 0:
            return max(minimum, min(7 * 86400.0, configured))
    except (TypeError, ValueError):
        logger.debug('[Video] bad TOFU_VIDEO_SCRATCH_TTL_S; using default')
    return max(minimum, 6 * 3600.0)


def reclaim_stale_scratch(path: str, *, now_s: float | None = None) -> int:
    """Remove a bounded batch of expired ``job_*`` scratch directories."""
    import shutil

    cutoff = float(time.time() if now_s is None else now_s) - video_scratch_ttl_s()
    removed = 0
    try:
        with os.scandir(path) as entries:
            for index, entry in enumerate(entries):
                if index >= SCRATCH_RECLAIM_SCAN_CAP:
                    break
                if not entry.name.startswith('job_'):
                    continue
                try:
                    if (entry.is_dir(follow_symlinks=False)
                            and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                        shutil.rmtree(entry.path, ignore_errors=True)
                        removed += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.debug('[Video] stale scratch probe failed: %s', exc)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.warning('[Video] scratch reclaim scan failed: %s', exc)
    if removed:
        logger.info('[Video] reclaimed %d stale scratch directories', removed)
    return removed


def frame_target_for_duration(duration_s: float) -> int:
    """Duration-tiered frame target (owner 2026-08-04): ≤60s→16, ≤600s→32, else 64.

    The target is the EXTRACTION count; the per-send count is further clamped
    per target model by ``lib.model_info.video_frame_budget`` at message-build
    time (frames are durable — the same video can be re-sent to a bigger-context
    model later and expand to more frames).
    """
    if duration_s <= 60.0:
        return 16
    if duration_s <= 600.0:
        return 32
    return FRAME_CEILING


def scene_score_threshold() -> float:
    """ffmpeg scene-cut score above which a frame counts as a cut (0.3 default).

    ``TOFU_VIDEO_SCENE_THRESHOLD`` overrides; 0 disables the scene pass.
    """
    try:
        v = float(os.environ.get('TOFU_VIDEO_SCENE_THRESHOLD', '') or 0)
        if v > 0:
            return v
    except (ValueError, TypeError) as e:
        logger.debug('[Video] bad TOFU_VIDEO_SCENE_THRESHOLD, using default: %s', e)
    return 0.3


def scratch_root() -> str:
    """Local-disk scratch root for ffmpeg decode (NEVER the FUSE mount).

    Honours ``TOFU_VIDEO_SCRATCH`` for operators who want a specific local
    disk; otherwise the system temp dir (``/tmp`` — local on Linux hosts).
    """
    override = os.environ.get('TOFU_VIDEO_SCRATCH', '').strip()
    base = override or tempfile.gettempdir()
    path = os.path.join(base, 'tofu-video-analysis')
    os.makedirs(path, exist_ok=True)
    reclaim_stale_scratch(path)
    return path
