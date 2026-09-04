"""lib/motion_video — Motion video (MG animation) generation pipeline.

Tofu's native absorb-and-surpass of the vibe-motion/auto-motion workflow
(SRT transcript → semantic storyboard → per-scene HyperFrames composition →
render → concat → final.mp4). See docs/modules/ingest_media.md.

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

from importlib import import_module

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


# A focused import such as ``lib.motion_video.runtime`` establishes only the
# durable task authority. Rendering recipes, binary probes, audio processing,
# and visual-quality modules remain request/worker loaded. The historical
# package-level API stays intact through explicit lazy attribute resolution.
_EXPORT_MODULES = {
    # Render-chain environment.
    'PINNED_HYPERFRAMES': 'lib.motion_video._env',
    'motion_root': 'lib.motion_video._env',
    'probe_env': 'lib.motion_video._env',
    'build_render_env': 'lib.motion_video._env',
    'hyperframes_bin': 'lib.motion_video._env',
    'ensure_hyperframes': 'lib.motion_video._env',
    'ensure_ffmpeg': 'lib.motion_video._env',
    'ensure_ffprobe': 'lib.motion_video._env',
    'ffmpeg_bin': 'lib.motion_video._env',
    'ffprobe_bin': 'lib.motion_video._env',
    'chrome_bin': 'lib.motion_video._env',
    # Source text and deterministic gates.
    'SrtEntry': 'lib.motion_video._srt',
    'parse_srt': 'lib.motion_video._srt',
    'parse_timestamp': 'lib.motion_video._srt',
    'format_timestamp': 'lib.motion_video._srt',
    'total_span': 'lib.motion_video._srt',
    'check_storyboard': 'lib.motion_video._gates',
    'check_scene_budget': 'lib.motion_video._gates',
    'NARRATION_CHARS_PER_SECOND': 'lib.motion_video._gates',
    'check_composition_html': 'lib.motion_video._gates',
    'check_text_fidelity': 'lib.motion_video._gates',
    'visible_text': 'lib.motion_video._gates',
    'probe_video': 'lib.motion_video._gates',
    'verify_spec': 'lib.motion_video._gates',
    'check_composition_fill': 'lib.motion_video._fill',
    'findings_for_fill': 'lib.motion_video._fill',
    'measure_fill': 'lib.motion_video._fill',
    'MIN_VERTICAL_SPAN': 'lib.motion_video._fill',
    'MAX_BOTTOM_DEAD_BAND': 'lib.motion_video._fill',
    # Quality telemetry and renderer verdicts.
    'GRAPHIC_EXTENSIONS': 'lib.motion_video._quality',
    'count_scene_graphics': 'lib.motion_video._quality',
    'asset_floor_findings': 'lib.motion_video._quality',
    'scene_telemetry': 'lib.motion_video._quality',
    'film_quality_summary': 'lib.motion_video._quality',
    'is_text_only_exempt': 'lib.motion_video._quality',
    'lint_project': 'lib.motion_video._render',
    'validate_project': 'lib.motion_video._render',
    'inspect_project': 'lib.motion_video._render',
    'check_project': 'lib.motion_video._render',
    'is_infra_category': 'lib.motion_video._render',
    'INFRA_CATEGORIES': 'lib.motion_video._render',
    'COMPOSITION_CATEGORIES': 'lib.motion_video._render',
    'ALL_CATEGORIES': 'lib.motion_video._render',
    'render_project': 'lib.motion_video._render',
    # Video/audio assembly.
    'concat_mp4s': 'lib.motion_video._concat',
    'burn_in_subtitles': 'lib.motion_video._concat',
    'NarrationAborted': 'lib.motion_video._audio',
    'synthesize_scene_narrations': 'lib.motion_video._audio',
    'concat_narrations': 'lib.motion_video._audio',
    'mux_audio_video': 'lib.motion_video._audio',
    'AUDIO_CONTRACT_VERSION': 'lib.motion_video._audio_cues',
    'normalise_audio_plan': 'lib.motion_video._audio_cues',
    'load_audio_plan': 'lib.motion_video._audio_cues',
    'audio_plan_errors': 'lib.motion_video._audio_cues',
    'audio_plan_summary': 'lib.motion_video._audio_cues',
    'audio_plan_template': 'lib.motion_video._audio_cues',
    'write_audio_attribution': 'lib.motion_video._audio_cues',
    'mix_audio_timeline': 'lib.motion_video._audio_cues',
    # Timeline, storyboard, subtitles, and HTML composition.
    'TIMELINE_CONTRACT_VERSION': 'lib.motion_video._timeline',
    'normalise_timeline_contract': 'lib.motion_video._timeline',
    'timeline_contract_errors': 'lib.motion_video._timeline',
    'transition_plan': 'lib.motion_video._timeline',
    'build_storyboard': 'lib.motion_video._storyboard',
    'SubtitleStyle': 'lib.motion_video._subtitle',
    'style_for_frame': 'lib.motion_video._subtitle',
    'safe_box': 'lib.motion_video._subtitle',
    'wrap_line': 'lib.motion_video._subtitle',
    'build_ass': 'lib.motion_video._subtitle',
    'MAX_LINES_PER_CUE': 'lib.motion_video._subtitle',
    'render_scene_html': 'lib.motion_video._template',
    'on_screen_capacity': 'lib.motion_video._template',
    'fit_font_px': 'lib.motion_video._template',
    'scene_on_screen': 'lib.motion_video._template',
    'MIN_FONT_PX': 'lib.motion_video._template',
}

_CHILD_MODULES = {
    '_asset_preflight', '_assets', '_audio', '_audio_cues', '_concat',
    '_craft', '_creative_plan', '_env', '_fill', '_fonts', '_gates',
    '_quality', '_recipe', '_render', '_runtime_assets', '_scene_author',
    '_shot_recipes', '_srt', '_storyboard', '_subtitle', '_template',
    '_timeline', 'engine', 'runtime',
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f'lib.motion_video.{name}'
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name)
    value = module if name in _CHILD_MODULES else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
