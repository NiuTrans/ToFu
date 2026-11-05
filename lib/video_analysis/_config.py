"""lib/video_analysis/_config.py — frame tunables and owned scratch lifecycle for video ingest.

Single home for every limit the video-upload pipeline enforces, so routes,
the frame extractor and tests all read the SAME numbers:

  * source duration and byte size are not admission quotas
  * duration-derived uniform sampling plus all nonredundant scene frames
  * frame geometry (1568px long side — ``lib.llm.body._images.
    _CLAUDE_STANDARD_IMAGE_MAX_PX``, at or under every Claude tier's cap,
    so a frame is never re-encoded across rounds → no prompt-cache churn)
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


def sniff_video_container(head: bytes) -> str | None:
    """Magic-bytes container sniff: 'mp4' (mp4/mov/m4v), 'webm' (webm/mkv),
    'avi', or None. Extension claims are NOT trusted — the payload decides."""
    if len(head) >= 12 and head[4:8] == b'ftyp':
        return 'mp4'
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'webm'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'avi'
    return None


# Audio-only files ``inspect_video`` reads (by EXTENSION; magic bytes are
# sniffed separately). Not upload formats: the upload route accepts VIDEO_EXTS.
AUDIO_EXTS: frozenset[str] = frozenset({
    '.mp3', '.m4a', '.aac', '.wav', '.ogg', '.oga', '.opus', '.flac', '.weba',
})


def sniff_audio_container(head: bytes) -> str | None:
    """Magic-bytes sniff for audio containers: 'mpeg' (an ID3 tag, or an MPEG
    audio/ADTS frame sync: 0xFF then a byte whose top three bits are set),
    'wav' (RIFF....WAVE), 'ogg' (OggS), 'flac' (fLaC), or None.

    ISO-BMFF (``ftyp``: m4a or mp4) stays 'mp4' in :func:`sniff_video_container`.
    ``FF FE`` is the UTF-16LE byte-order mark of a text file, not a frame sync.
    """
    if head[:3] == b'ID3':
        return 'mpeg'
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and head[1] != 0xFE:
        return 'mpeg'
    if len(head) >= 12 and head[:4] == b'RIFF' and head[8:12] == b'WAVE':
        return 'wav'
    if head[:4] == b'OggS':
        return 'ogg'
    if head[:4] == b'fLaC':
        return 'flac'
    return None

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


def frame_target_for_duration(duration_s: float, source_fps: float = 0.0) -> int:
    """Uniform samples grow with duration: at least 16, at most 10s apart.

    A known source frame rate bounds the target by the frames that physically
    exist in the span, so a short clip never yields duplicated frames.
    This is a sampling cadence, never a ceiling on scene evidence or a source
    admission decision. Finite positive duration is validated by the pipeline.
    """
    target = max(16, math.ceil(duration_s / 10.0))
    if source_fps > 0:
        target = min(target, max(1, int(source_fps * duration_s)))
    return target


def scene_score_threshold() -> float:
    """ffmpeg scene-cut score above which a frame counts as a cut (0.3 default).

    ``TOFU_VIDEO_SCENE_THRESHOLD`` overrides; 0 disables the scene pass.
    Unset, unparsable, negative or non-finite values keep the default.
    """
    raw = os.environ.get('TOFU_VIDEO_SCENE_THRESHOLD', '').strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if math.isfinite(value) and value >= 0:
            return value
        logger.debug('[Video] bad TOFU_VIDEO_SCENE_THRESHOLD %r; using default', raw)
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
