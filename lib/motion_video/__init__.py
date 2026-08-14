"""lib/motion_video — Motion video (MG animation) generation pipeline.

Tofu's native absorb-and-surpass of the vibe-motion/auto-motion workflow
(SRT transcript → semantic storyboard → per-scene HyperFrames composition →
render → concat → final.mp4). See docs/MOTION_VIDEO_DESIGN.md.

Layers:

  * :mod:`._env`    — render-chain environment (node / hyperframes CLI /
                      ffmpeg / ffprobe / Chrome), incl. the managed
                      auto-install of the pinned HyperFrames CLI.
  * :mod:`._srt`    — SRT parsing (millisecond precision).
  * :mod:`._gates`  — zero-LLM validation: storyboard timeline gates,
                      composition static contract, media spec verification.
  * :mod:`._render` — HyperFrames CLI subprocess wrapper (env injection,
                      timeout, cooperative abort, failure classification).
  * :mod:`._concat` — scene normalization + concat → final.mp4 (atomic).

The ``guide/`` directory holds the vendored in-tree knowledge the chat
agent reads (workflow + composition contract + skeleton). The full
vibe-motion skill packs (29 motion rules, 13 blueprints, 20+ design frame
presets) are installable from the Settings → Skills catalog.
"""

from __future__ import annotations

from lib.motion_video._audio import (
    NarrationAborted,
    concat_narrations,
    mux_audio_video,
    synthesize_scene_narrations,
)
from lib.motion_video._audio_cues import (
    AUDIO_CONTRACT_VERSION,
    audio_plan_errors,
    audio_plan_summary,
    audio_plan_template,
    load_audio_plan,
    mix_audio_timeline,
    normalise_audio_plan,
    write_audio_attribution,
)
from lib.motion_video._concat import burn_in_subtitles, concat_mp4s
from lib.motion_video._env import (
    PINNED_HYPERFRAMES,
    build_render_env,
    chrome_bin,
    ensure_ffmpeg,
    ensure_ffprobe,
    ensure_hyperframes,
    ffmpeg_bin,
    ffprobe_bin,
    hyperframes_bin,
    motion_root,
    probe_env,
)
from lib.motion_video._fill import (
    MAX_BOTTOM_DEAD_BAND,
    MIN_VERTICAL_SPAN,
    check_composition_fill,
    findings_for_fill,
    measure_fill,
)
from lib.motion_video._gates import (
    NARRATION_CHARS_PER_SECOND,
    check_composition_html,
    check_scene_budget,
    check_storyboard,
    check_text_fidelity,
    probe_video,
    verify_spec,
    visible_text,
)
from lib.motion_video._quality import (
    GRAPHIC_EXTENSIONS,
    asset_floor_findings,
    count_scene_graphics,
    film_quality_summary,
    is_text_only_exempt,
    scene_telemetry,
)
from lib.motion_video._render import (
    ALL_CATEGORIES,
    COMPOSITION_CATEGORIES,
    INFRA_CATEGORIES,
    check_project,
    inspect_project,
    is_infra_category,
    lint_project,
    render_project,
    validate_project,
)
from lib.motion_video._srt import (
    SrtEntry,
    format_timestamp,
    parse_srt,
    parse_timestamp,
    total_span,
)
from lib.motion_video._storyboard import build_storyboard
from lib.motion_video._subtitle import (
    MAX_LINES_PER_CUE,
    SubtitleStyle,
    build_ass,
    safe_box,
    style_for_frame,
    wrap_line,
)
from lib.motion_video._template import (
    MIN_FONT_PX,
    fit_font_px,
    on_screen_capacity,
    render_scene_html,
    scene_on_screen,
)
from lib.motion_video._timeline import (
    TIMELINE_CONTRACT_VERSION,
    normalise_timeline_contract,
    timeline_contract_errors,
    transition_plan,
)

__all__ = [
    'PINNED_HYPERFRAMES',
    'motion_root',
    'probe_env',
    'build_render_env',
    'hyperframes_bin',
    'ensure_hyperframes',
    'ensure_ffmpeg',
    'ensure_ffprobe',
    'ffmpeg_bin',
    'ffprobe_bin',
    'chrome_bin',
    'SrtEntry',
    'parse_srt',
    'parse_timestamp',
    'format_timestamp',
    'total_span',
    'check_storyboard',
    'check_scene_budget',
    'NARRATION_CHARS_PER_SECOND',
    'check_composition_html',
    'check_text_fidelity',
    'check_composition_fill',
    'findings_for_fill',
    'measure_fill',
    'MIN_VERTICAL_SPAN',
    'MAX_BOTTOM_DEAD_BAND',
    'GRAPHIC_EXTENSIONS',
    'count_scene_graphics',
    'asset_floor_findings',
    'scene_telemetry',
    'film_quality_summary',
    'is_text_only_exempt',
    'visible_text',
    'probe_video',
    'verify_spec',
    'lint_project',
    'validate_project',
    'inspect_project',
    'check_project',
    'is_infra_category',
    'INFRA_CATEGORIES',
    'COMPOSITION_CATEGORIES',
    'ALL_CATEGORIES',
    'render_project',
    'concat_mp4s',
    'burn_in_subtitles',
    'NarrationAborted',
    'synthesize_scene_narrations',
    'concat_narrations',
    'mux_audio_video',
    'AUDIO_CONTRACT_VERSION',
    'normalise_audio_plan',
    'load_audio_plan',
    'audio_plan_errors',
    'audio_plan_summary',
    'audio_plan_template',
    'write_audio_attribution',
    'mix_audio_timeline',
    'TIMELINE_CONTRACT_VERSION',
    'normalise_timeline_contract',
    'timeline_contract_errors',
    'transition_plan',
    'build_storyboard',
    'SubtitleStyle',
    'style_for_frame',
    'safe_box',
    'wrap_line',
    'build_ass',
    'MAX_LINES_PER_CUE',
    'render_scene_html',
    'on_screen_capacity',
    'fit_font_px',
    'scene_on_screen',
    'MIN_FONT_PX',
]
