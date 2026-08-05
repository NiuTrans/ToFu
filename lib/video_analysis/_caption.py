"""lib/video_analysis/_caption.py — the visual storyboard (vision-slot narration).

The model-agnostic completion of video upload (owner ruling 2026-08-04:
「一定要用 gemini 吗?用户有什么模型用什么模型不可以么?」 — NO vendor-specific
native-video path). ONE batched vision call at PIPELINE time narrates the
sampled frame strip into a compact text storyboard, stored on the video
payload. At send time the transform picks:

  * chat model HAS vision  → raw frames ride the request (storyboard unused);
  * chat model is text-only → storyboard + transcript carry the video,
    so a text-only chat model still gets the visual channel whenever ANY
    vision-capable slot exists in the pool.

This is the classic VideoAgent decomposition (frame sampler + image VLM +
LLM) with zero new dependencies: routing rides
``dispatch_chat(capability='vision')`` — the same slot pool, fallback chain
and cooldown machinery the main chat path uses. Pipeline-time (not send-time)
by design: the storyboard is a property of (video, vision pool), not of the
chat model — so it is generated once, rides the durable payload, and never
adds latency or cost to a send / preview / compaction pass.

Statuses: ``disabled`` (TOFU_VIDEO_STORYBOARD=0) / ``no_frames`` /
``no_vision_slot`` / ``failed`` / ``ok``. Every non-ok status degrades
gracefully — the video still works wherever it worked before.
"""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.model_info import video_frame_budget
from lib.video_analysis._frames import _fmt_video_ts, _thin_frames

logger = get_logger(__name__)


def storyboard_enabled() -> bool:
    """Kill switch: ``TOFU_VIDEO_STORYBOARD=0`` skips the storyboard pass."""
    return os.environ.get('TOFU_VIDEO_STORYBOARD', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _vision_slot_models() -> list[str]:
    """Model ids of configured vision-capable slots (best score first).

    Availability probe only — the actual pick happens inside dispatch_chat.
    OAuth subscription slots are INCLUDED (they are valid vision chat
    targets through the normal outbound bridge).
    """
    try:
        from lib.llm_dispatch.factory import get_dispatcher
        dispatcher = get_dispatcher()
        dispatcher.initialize()
    except Exception as e:
        logger.warning('[VideoStoryboard] dispatcher unavailable: %s', e)
        return []
    slots = [s for s in dispatcher.slots
             if 'vision' in (getattr(s, 'capabilities', None) or set())]
    slots.sort(key=lambda s: s.score())
    return [s.model for s in slots]


def storyboard_for_frames(frames: list[dict], *, name: str = 'video',
                          duration_s: float = 0.0) -> dict:
    """Narrate persisted frames → ``{text, status, model}``. Never raises.

    ``frames`` are the durable ``[{url, t, bytes}]`` entries produced by
    ``persist_frames`` — the vision call reuses the standard image path
    (``_validate_image_blocks`` resolves the local /api/images/ URLs).
    The frame set is thinned to the pool's own per-model budget, so the
    storyboard call never exceeds what a vision model can take.
    """
    if not storyboard_enabled():
        return {'text': '', 'status': 'disabled', 'model': ''}
    if not frames:
        return {'text': '', 'status': 'no_frames', 'model': ''}

    models = _vision_slot_models()
    if not models:
        logger.info('[VideoStoryboard] no vision slot configured — skipping')
        return {'text': '', 'status': 'no_vision_slot', 'model': ''}

    avg_bytes = int(sum(int(f.get('bytes') or 0) for f in frames)
                    / max(len(frames), 1))
    budget = video_frame_budget(models[0], avg_frame_bytes=avg_bytes)
    kept = _thin_frames(frames, budget)

    blocks: list[dict] = []
    for fr in kept:
        blocks.append({'type': 'image_url', 'image_url': {'url': fr['url']}})
        blocks.append({'type': 'text',
                       'text': f'[frame at {_fmt_video_ts(fr.get("t") or 0)}]'})
    dur_txt = f'{duration_s:.0f}s' if duration_s else 'unknown-length'
    blocks.append({'type': 'text', 'text': (
        f'You are given {len(kept)} frames sampled from a {dur_txt} video '
        f'"{name}", each labeled with its timestamp. Write a compact visual '
        'storyboard of the video: for EVERY frame, in timestamp order, output '
        'one line `- [MM:SS] <what is visible — key objects, people, actions, '
        'scene changes, and any legible on-screen text transcribed verbatim>`. '
        'After the last frame, output one line `Overall: <one-sentence arc of '
        'the video>`. Be strictly factual — describe only what is visible; '
        'never invent content.')})

    try:
        from lib.llm_dispatch import dispatch_chat
        content, usage = dispatch_chat(
            [{'role': 'user', 'content': blocks}],
            capability='vision', temperature=0, max_tokens=4096,
            log_prefix='[VideoStoryboard]')
    except Exception as e:
        logger.warning('[VideoStoryboard] vision call failed: %s', e)
        return {'text': '', 'status': 'failed', 'model': ''}

    text = (content or '').strip()
    if not text:
        logger.warning('[VideoStoryboard] empty storyboard from vision call')
        return {'text': '', 'status': 'failed', 'model': ''}
    model = ''
    if isinstance(usage, dict):
        model = (usage.get('_dispatch') or {}).get('model') or usage.get('model') or ''
    logger.info('[VideoStoryboard] %d/%d frames → %d chars via %s',
                len(kept), len(frames), len(text), model or '?')
    return {'text': text, 'status': 'ok', 'model': model}
