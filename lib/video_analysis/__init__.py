"""lib/video_analysis — video understanding: probe → frames → transcript.

Turns an uploaded video into owner-scoped Knowledge evidence: one durable
original, bounded timestamped JPEG frames, a visual storyboard, and an optional
audio transcript. Turns retain only the media reference; model requests expand
the evidence under a request-local budget. The read-only ``inspect_video``
tool reuses the same probe/frame/transcript owners on any local video or
audio-only file.

Facade package — every public name resolves to a sub-module implementation:

  * ``_config``   — caps/tiers/scratch (TOFU_VIDEO_* env knobs)
  * ``_store``    — the live-status registry (json_store, atomic)
  * ``_frames``   — uniform+scene frame extraction → durable /api/images/ URLs
  * ``_audio``    — audio-track extraction → lib.transcription
  * ``_pipeline`` — background-thread orchestration
  * ``_inspect``  — on-demand, non-persisting inspection for ``inspect_video``

Tests monkeypatch seams on THIS package (facade-aware resolution).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

from lib.video_analysis._config import (  # noqa: E402,F401
    AUDIO_EXTS,
    FRAME_JPEG_Q,
    FRAME_LONG_SIDE_PX,
    VIDEO_EXTS,
    frame_target_for_duration,
    scene_score_threshold,
    scratch_root,
    sniff_audio_container,
    sniff_video_container,
    video_analysis_enabled,
    video_scratch_ttl_s,
)
from lib.video_analysis._store import (  # noqa: E402,F401
    complete_record,
    create_record,
    fail_record,
    get_record,
    register_video_asset,
    resolve_owned_video_asset,
    set_phase,
    update_record,
)
from lib.video_analysis._frames import (  # noqa: E402,F401
    extract_frames,
    persist_frames,
)
from lib.video_analysis._audio import transcribe_track  # noqa: E402,F401
from lib.video_analysis._caption import (  # noqa: E402,F401
    storyboard_enabled,
    storyboard_for_frames,
)
from lib.video_analysis._pipeline import (  # noqa: E402,F401
    reserve_processing_slot,
    start_processing,
    videos_dir,
)
from lib.video_analysis._inspect import (  # noqa: E402,F401
    VideoInspectError,
    inspect_video_file,
)

__all__ = [
    'FRAME_JPEG_Q', 'FRAME_LONG_SIDE_PX',
    'VIDEO_EXTS', 'sniff_video_container', 'AUDIO_EXTS', 'sniff_audio_container',
    'frame_target_for_duration', 'scene_score_threshold', 'scratch_root',
    'video_analysis_enabled',
    'video_scratch_ttl_s',
    'create_record', 'get_record', 'register_video_asset',
    'resolve_owned_video_asset', 'update_record', 'set_phase',
    'complete_record', 'fail_record',
    'extract_frames', 'persist_frames', 'transcribe_track',
    'storyboard_enabled', 'storyboard_for_frames',
    'reserve_processing_slot', 'start_processing',
    'videos_dir',
    'VideoInspectError', 'inspect_video_file',
]
