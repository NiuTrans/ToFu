# HOT_PATH — functions in this module are called per-request.
"""lib/model_info/_video.py — per-model VIDEO FRAME budget (send-time clamp).

The video-ingest pipeline (lib/video_analysis) extracts up to 64 durable
frames per video at UPLOAD time, model-agnostically. How many of those frames
a given request may carry is decided HERE, per target model, at message-build
time — the same conversation re-sent to a bigger-window model expands to more
frames, a smaller window thins to fewer, uniformly (temporal coverage kept).

Four clamps, minimum wins (owner ruling 2026-08-04: the frame budget must be
model-aware, not just duration-tiered):

  1. VISION GATE   — a model without the ``vision`` capability gets ZERO
                     frames (the transcript still flows as text).
  2. FAMILY CAP    — Claude's Messages API hard-rejects requests with more
                     than 100 images; we budget 40 per video so two videos
                     plus a handful of regular images stay under the wire.
  3. CONTEXT SHARE — frames may consume at most 30% of the (learned or
                     default) context window at ~1500 tokens/frame (Claude's
                     1.15MP tokenizer is the worst case; GPT-4o ≈ 1100,
                     Gemini 258 — budgeting for the worst protects all).
  4. WIRE BYTES    — when the extraction stats are known, total frame bytes
                     stay under 8 MiB (the gateway's 413 body-size wall is
                     the empirically observed failure — see the
                     ``_shrink_upload_image`` rationale in routes/upload.py).

Depends on ._capabilities (vision probe) and ._family (is_claude) — acyclic.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.model_info._capabilities import model_supports_vision
from lib.model_info._family import is_claude

logger = get_logger(__name__)

#: Mirrors lib.video_analysis.FRAME_CEILING — duplicated BY VALUE to keep the
#: import direction one-way (video_analysis → model_info, never the reverse).
#: tests/test_video_analysis.py pins the two literals equal.
_EXTRACTION_CEILING = 64

#: Per-frame token estimate (worst-case Claude 1568px tokenizer ≈ 1.15MP).
_TOKENS_PER_FRAME = 1500

#: Max share of the context window video frames may occupy.
_CONTEXT_SHARE = 0.30

#: Assumed context window when nothing is learned/configured for the model.
_DEFAULT_CONTEXT_TOKENS = 128_000

#: Total inline frame bytes per video stay under this (gateway 413 headroom).
_WIRE_BYTES_CAP = 8 * 1024 * 1024

#: A video with fewer frames than this stops being a video — the floor, so
#: tiny-context models still get *some* temporal coverage.
_FLOOR = 4

#: Family image-count caps PER VIDEO (aggregate accounting across the whole
#: request lives in conv_message_builder._transform).
_CLAUDE_PER_VIDEO_CAP = 40


def video_frame_budget(model: str, *, avg_frame_bytes: int = 0) -> int:
    """How many frames of ONE video the given model may receive in a request.

    Returns 0 for non-vision models (frames stripped; transcript still flows).
    Never raises — any lookup failure degrades to the conservative default.
    """
    if not model_supports_vision(model):
        logger.info('[ModelInfo] video_frame_budget(%s)=0 — no vision cap', model)
        return 0

    cap = _CLAUDE_PER_VIDEO_CAP if is_claude(model) else _EXTRACTION_CEILING

    # Context-share clamp
    ctx = _DEFAULT_CONTEXT_TOKENS
    try:
        from lib.context_limits import lookup_learned_context_limit
        learned = lookup_learned_context_limit(None, model)
        if learned:
            ctx = learned
    except Exception as e:
        logger.debug('[ModelInfo] learned context lookup failed for %s: %s', model, e)
    ctx_frames = max(_FLOOR, int(ctx * _CONTEXT_SHARE // _TOKENS_PER_FRAME))
    cap = min(cap, ctx_frames)

    # Wire-bytes clamp (only when extraction stats are available)
    if avg_frame_bytes > 0:
        wire_frames = max(_FLOOR, _WIRE_BYTES_CAP // avg_frame_bytes)
        cap = min(cap, wire_frames)

    budget = max(_FLOOR, min(cap, _EXTRACTION_CEILING))
    logger.debug('[ModelInfo] video_frame_budget(%s)=%d (ctx=%d, avg_bytes=%d)',
                 model, budget, ctx, avg_frame_bytes)
    return budget


#: Aggregate image-count ceiling across the WHOLE request, by family.
#: Only Claude documents a hard per-request count (100) — margin to 90.
#: Other families return None (no documented count limit; the byte clamps
#: above are their effective bound).
def aggregate_image_cap(model: str) -> int | None:
    return 90 if is_claude(model) else None
