"""lib/video_analysis/_config.py — frame tunables and owned scratch lifecycle for video ingest.

Single home for every limit the video-upload pipeline enforces, so routes,
the frame extractor and tests all read the SAME numbers:

  * source duration and byte size are not admission quotas
  * duration-derived uniform sampling plus all nonredundant scene frames
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
import math
import json
import tempfile
import time

from lib.log import get_logger

logger = get_logger(__name__)

# Accepted upload containers (by EXTENSION; magic bytes are sniffed separately).
VIDEO_EXTS: frozenset[str] = frozenset({
    '.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi',
})

#: Long-side pixel cap applied AT EXTRACTION (ffmpeg scale filter), so frames
#: are born cache-stable and never need a retroactive re-encode.
FRAME_LONG_SIDE_PX = 1568

#: ffmpeg -q:v for frame JPEGs (≈ quality 80 — text-on-screen stays readable,
#: a 1568px frame lands ~100-200KB).
FRAME_JPEG_Q = 4

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


def video_scratch_ttl_s() -> float:
    """TTL for reconstructible local decode state left by a killed process."""
    minimum = 3600.0
    try:
        configured = float(
            os.environ.get('TOFU_VIDEO_SCRATCH_TTL_S', '') or 0)
        if configured > 0:
            return max(minimum, min(7 * 86400.0, configured))
    except (TypeError, ValueError):
        logger.debug('[Video] bad TOFU_VIDEO_SCRATCH_TTL_S; using default')
    return max(minimum, 6 * 3600.0)


def mark_scratch_owner(path: str) -> None:
    """Stamp the upload's process incarnation before any long-running I/O."""
    from lib.process_owner import process_owner
    with open(os.path.join(path, '.process-owner.json'), 'w') as output:
        json.dump(process_owner(), output)


def _scratch_owner_allows_reclaim(path: str) -> bool:
    from lib.process_owner import owner_state
    try:
        with open(os.path.join(path, '.process-owner.json')) as source:
            owner = json.load(source)
    except FileNotFoundError:
        return True  # Legacy directories retain their existing grace policy.
    except (OSError, ValueError):
        return False
    return owner_state(owner) == 'dead'


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
                            and entry.stat(follow_symlinks=False).st_mtime < cutoff
                            and _scratch_owner_allows_reclaim(entry.path)):
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
    """Uniform samples grow with duration: at least 16, at most 10s apart.

    This is a sampling cadence, never a ceiling on scene evidence or a source
    admission decision. Finite positive duration is validated by the pipeline.
    """
    return max(16, math.ceil(duration_s / 10.0))


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
