"""lib/video_analysis — upload-time video ingest: probe → frames → transcript.

Turns an uploaded video into a set of durable, timestamped JPEG frames (in the
regular uploads/images store) plus an optional audio-track transcript (via the
existing lib.transcription slot chain), so ANY vision-capable chat model can
analyze video through the unchanged image path — the "storyboard + script"
pattern from the 2026-08-04 open-source survey (P1). Gemini-native passthrough
(P2) builds on the persisted original file.

Facade package — every public name resolves to a sub-module implementation:

  * ``_config``   — caps/tiers/scratch (TOFU_VIDEO_* env knobs)
  * ``_store``    — the live-status registry (json_store, atomic)
  * ``_frames``   — uniform+scene frame extraction → durable /api/images/ URLs
  * ``_audio``    — audio-track extraction → lib.transcription
  * ``_pipeline`` — background-thread orchestration

Tests monkeypatch seams on THIS package (facade-aware resolution).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

from lib.video_analysis._config import (  # noqa: E402,F401
    FRAME_CEILING,
    FRAME_JPEG_Q,
    FRAME_LONG_SIDE_PX,
    TRANSCRIPT_CHAR_CAP,
    VIDEO_EXTS,
    frame_target_for_duration,
    scene_score_threshold,
    scratch_root,
    video_analysis_enabled,
    video_max_bytes,
    video_max_duration_s,
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
    start_processing,
    videos_dir,
)

__all__ = [
    'FRAME_CEILING', 'FRAME_JPEG_Q', 'FRAME_LONG_SIDE_PX', 'TRANSCRIPT_CHAR_CAP',
    'VIDEO_EXTS',
    'frame_target_for_duration', 'scene_score_threshold', 'scratch_root',
    'video_analysis_enabled', 'video_max_bytes', 'video_max_duration_s',
    'create_record', 'get_record', 'register_video_asset',
    'resolve_owned_video_asset', 'update_record', 'set_phase',
    'complete_record', 'fail_record',
    'extract_frames', 'persist_frames', 'transcribe_track',
    'storyboard_enabled', 'storyboard_for_frames',
    'start_processing', 'videos_dir',
]
