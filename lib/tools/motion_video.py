"""lib/tools/motion_video.py — Motion-video tool definitions.

The chat-agent surface of the motion-video pipeline
(docs/MOTION_VIDEO_DESIGN.md): the agent storyboards + authors scene
compositions itself, and these tools are the deterministic machinery around
it (env bootstrap, zero-LLM gates, render, probe, concat). Execution lives
in ``lib/tasks_pkg/handlers/motion_video.py`` on top of
:mod:`lib.motion_video`.

Workdir convention (taught to the agent by ``lib/motion_video/guide/``):
``.tofu/motion_video/<slug>/`` inside the current project.
"""

from lib.log import get_logger

logger = get_logger(__name__)

MOTION_VIDEO_TOOL_NAMES = {
    'motion_video_env_check',
    'motion_video_storyboard_check',
    'motion_video_check',
    'motion_video_render',
    'motion_video_probe',
    'motion_video_concat',
    'motion_video_narrate',
    'motion_video_mux',
}

_GUIDE_HINT = (
    'Before authoring your first scene, read the vendored guides '
    'lib/motion_video/guide/WORKFLOW.md and COMPOSITION_CONTRACT.md '
    '(absolute paths under the tofu install) and start each scene from '
    'lib/motion_video/guide/skeleton.html.'
)

MOTION_VIDEO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "motion_video_env_check",
            "description": (
                "Check (and optionally bootstrap) the motion-video render environment: "
                "Node>=22, the HyperFrames CLI (auto-installed pinned version when missing), "
                "ffmpeg, ffprobe (optional), and a headless Chrome. Call this first when a "
                "motion_video_* tool reports env_missing, or before your first render on a "
                "fresh deployment. " + _GUIDE_HINT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "install": {
                        "type": "boolean",
                        "description": "When true (default), auto-install the pinned HyperFrames "
                                       "CLI into the tofu data dir if it is missing. First "
                                       "install downloads npm packages and can take a minute."
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "motion_video_storyboard_check",
            "description": (
                "Zero-LLM gate: validate a scenes.json storyboard against its SRT transcript — "
                "required fields, monotonic contiguity, FULL coverage of the SRT span, and "
                "scene-duration sum equal to the span within ±0.1s. MANDATORY before rendering "
                "any scene. Returns a list of human-readable errors (empty = pass)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "srt_path": {
                        "type": "string",
                        "description": "Path to the SRT transcript (project-relative or absolute)."
                    },
                    "scenes_path": {
                        "type": "string",
                        "description": "Path to scenes.json — a list of "
                                       "{id, start, end, text, visual?} with float seconds."
                    },
                    "tolerance": {
                        "type": "number",
                        "description": "Coverage tolerance in seconds (default 0.1)."
                    }
                },
                "required": ["srt_path", "scenes_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "motion_video_check",
            "description": (
                "Run the HyperFrames static gates (lint + headless-Chrome validate + layout "
                "inspect) on one scene project dir. MANDATORY before rendering each scene. "
                "On failure returns categorized errors with upstream fix hints — repair the "
                "composition in place and re-check (max 2 repair rounds)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Scene project dir holding index.html "
                                       "(e.g. .tofu/motion_video/<slug>/scenes/scene-001)."
                    }
                },
                "required": ["project_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "motion_video_render",
            "description": (
                "Render one scene project to MP4 via HyperFrames (headless Chrome, deterministic). "
                "Cost is ~3.5x realtime on this host (a 10s scene ≈ 35s). Use quality 'draft' "
                "while iterating, 'standard' for delivery, 'high' for final takes. After "
                "rendering, verify with motion_video_probe (resolution/fps/duration/silence)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_dir": {
                        "type": "string",
                        "description": "Scene project dir holding index.html."
                    },
                    "output": {
                        "type": "string",
                        "description": "Output MP4 path (project-relative or absolute, "
                                       "e.g. .tofu/motion_video/<slug>/scenes/scene-001/scene-001.mp4)."
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["draft", "standard", "high"],
                        "description": "Render quality (default 'standard')."
                    },
                    "fps": {
                        "type": "integer",
                        "enum": [24, 30, 60],
                        "description": "Optional frame-rate override (default 30)."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Wall-clock seconds before the render is killed "
                                       "(default 1800; 0 = no limit)."
                    }
                },
                "required": ["project_dir", "output"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "motion_video_probe",
            "description": (
                "Probe a media file (ffprobe, ffmpeg fallback): codec, resolution, fps, "
                "duration, audio-track presence. Use after every render and after concat "
                "to verify specs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Media file path (project-relative or absolute)."
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "motion_video_concat",
            "description": (
                "Assemble final.mp4 from ordered scene MP4s. Uniform specs concat losslessly "
                "(-c copy); mismatched scenes are re-encode-normalized to the first scene's "
                "spec. Optional transitions execute real FFmpeg xfade edits. For every overlap, "
                "the outgoing scene MUST already include an equal visual tail handle; then the "
                "final program duration stays aligned with narration/SRT. Output is atomic and "
                "post-verified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "inputs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered scene MP4 paths."
                    },
                    "output": {
                        "type": "string",
                        "description": "Final MP4 path (e.g. .tofu/motion_video/<slug>/final.mp4)."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Wall-clock seconds (default 1800)."
                    },
                    "transitions": {
                        "type": "array",
                        "description": "Optional ordered N-1 boundary plan. Zero duration is a "
                                       "hard cut; non-zero boundaries consume an equal outgoing "
                                       "visual handle from the previous clip.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "duration_s": {"type": "number", "minimum": 0,
                                               "maximum": 0.8},
                                "ffmpeg": {"type": "string", "enum": [
                                    "", "fade", "wipeleft", "wiperight", "wipeup",
                                    "wipedown", "slideleft", "slideright", "slideup",
                                    "slidedown"]}
                            },
                            "required": ["duration_s", "ffmpeg"]
                        }
                    }
                },
                "required": ["inputs", "output"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "motion_video_narrate",
            "description": (
                "P2 音画合成 step 1: synthesize per-scene TTS narration WAVs from a "
                "storyboard scenes.json. Returns an alignment manifest — in the default "
                "'loose' mode each scene's target_duration = max(srt, audio + tail_pad); "
                "when target_duration exceeds the SRT duration you MUST update that scene's "
                "data-duration before rendering. Degrades gracefully (ok=false, "
                "degraded=true) when no TTS slot is configured — deliver the silent video."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scenes_path": {
                        "type": "string",
                        "description": "Path to scenes.json (the storyboard-checked list)."
                    },
                    "out_dir": {
                        "type": "string",
                        "description": "Directory for per-scene narration WAVs "
                                       "(e.g. .tofu/motion_video/<slug>/audio)."
                    },
                    "voice": {
                        "type": "string",
                        "description": "Optional TTS voice override (default: data/config/tts.json)."
                    },
                    "speed": {
                        "type": "number",
                        "description": "Optional TTS speed override (e.g. 1.1)."
                    },
                    "alignment": {
                        "type": "string",
                        "enum": ["loose", "strict"],
                        "description": "'loose' (default): scene duration follows the audio. "
                                       "'strict': SRT span is fixed; over-long narration is "
                                       "reported as overflow for you to shorten the text."
                    }
                },
                "required": ["scenes_path", "out_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "motion_video_mux",
            "description": (
                "Final audio step. With audio_plan_path, stage and hash licensed local BGM/SFX, "
                "resolve cues against scenes/beat time, duck BGM under narration, mix to -14 "
                "LUFS, and emit audio_plan.json + audio_attribution.txt. Without a plan, mux the "
                "single narration track using the legacy loudnorm path. Runtime audio downloads "
                "are forbidden. Post-verified: audio present and program duration preserved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "video": {
                        "type": "string",
                        "description": "Silent final MP4 (from motion_video_concat)."
                    },
                    "audio": {
                        "type": "string",
                        "description": "Optional narration track. Required when no audio plan is "
                                       "provided; optional for BGM/SFX-only delivery."
                    },
                    "output": {
                        "type": "string",
                        "description": "Deliverable MP4 path (e.g. .tofu/motion_video/<slug>/final.mp4)."
                    },
                    "loudnorm": {
                        "type": "boolean",
                        "description": "Apply EBU R128 loudness normalization (default true)."
                    },
                    "audio_plan_path": {
                        "type": "string",
                        "description": "Optional motion-audio-v1 JSON, relative to the project "
                                       "or absolute. Its asset paths resolve beside the plan."
                    },
                    "scenes_path": {
                        "type": "string",
                        "description": "scenes.json carrying motion-timeline-v1 fields; required "
                                       "for scene-relative SFX cues."
                    }
                },
                "required": ["video", "output"],
            },
        },
    },
]

__all__ = ['MOTION_VIDEO_TOOL_NAMES', 'MOTION_VIDEO_TOOLS']
