"""lib/video_analysis/_caption.py — the visual storyboard (vision-slot narration).

The model-agnostic completion of video upload (owner ruling 2026-08-04:
「一定要用 gemini 吗?用户有什么模型用什么模型不可以么?」 — NO vendor-specific
native-video path). ONE batched vision call at PIPELINE time narrates the
sampled frame strip into a compact text storyboard, stored on the video
payload. At send time the transform picks:

  * chat model HAS vision  → raw frames ride the request (storyboard unused);
  * chat model is text-only → storyboard + transcript carry the video,
    so a text-only chat model still gets the visual channel whenever ANY
    vision-capable route exists for the attachment owner.

This is the classic VideoAgent decomposition (frame sampler + image VLM +
LLM) with zero new dependencies: routing rides
``dispatch_chat(capability='vision')`` inside a bounded, owner-scoped v2 route
group, with the same fallback and cooldown machinery the main chat path uses.
Pipeline-time (not send-time) by design: the storyboard is a property of the
video and its owner's runnable vision routes, not of the eventual chat model —
so it is generated once, rides the durable payload, and never adds latency or
cost to a send / preview / compaction pass.

Statuses: ``disabled`` (TOFU_VIDEO_STORYBOARD=0) / ``no_frames`` /
``no_vision_slot`` / ``failed`` / ``ok``. Every non-ok status degrades
gracefully — the video still works wherever it worked before.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

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

    Provider-only availability probe — the actual pick happens in dispatch_chat.
    Owner-facing calls enter this function under a request-group hard pin.
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
    from lib.llm_dispatch.provider_pin import get_pinned_provider
    pinned_provider = get_pinned_provider()
    slots = [s for s in dispatcher.slots
             if 'vision' in (getattr(s, 'capabilities', None) or set())
             and (not pinned_provider or s.provider_id == pinned_provider)]
    slots.sort(key=lambda s: s.score())
    return [getattr(s, 'logical_model', '') or s.model for s in slots]


def storyboard_for_frames(frames: list[dict], *, name: str = 'video',
                          duration_s: float = 0.0,
                          owner_user_id: int | None = None,
                          tenant_id: str | None = None) -> dict:
    """Narrate persisted frames → ``{text, status, model}``. Never raises.

    ``frames`` may be durable ``[{url, t, bytes}]`` entries from legacy
    conversations or local scratch ``[{path, t}]`` entries from the unified
    attachment pipeline. Scratch images become request-local data URIs and
    are never copied into a second image store.
    The frame set is thinned to the pool's own per-model budget, so the
    storyboard call never exceeds what a vision model can take.
    """
    if not storyboard_enabled():
        return {'text': '', 'status': 'disabled', 'model': ''}
    if not frames:
        return {'text': '', 'status': 'no_frames', 'model': ''}

    # Validation and cheap kill switches precede storage/routing access. The
    # recursive provider-only path sees only the freshly minted hard-pinned
    # group, so a background upload can never borrow another owner's vision
    # credential from the process dispatcher.
    if owner_user_id is not None:
        import lib.model_routing as routing
        from lib.llm_dispatch.provider_pin import provider_pin

        route_group = None
        try:
            _model, route_group = routing.mint_capability_slot_group(
                routing.ModelRoutingRepository(),
                routing.OwnerBoundary.create(owner_user_id, tenant_id),
                'vision',
                owner_tag=f'video-storyboard:{owner_user_id}',
                max_candidates=8,
            )
            with provider_pin(route_group.pin_id):
                return storyboard_for_frames(
                    frames, name=name, duration_s=duration_s)
        except routing.ModelRoutingError as exc:
            status = ('no_vision_slot'
                      if exc.kind == 'model_route_unavailable' else 'failed')
            logger.warning('[VideoStoryboard] owner route unavailable: %s', exc)
            return {'text': '', 'status': status, 'model': ''}
        finally:
            routing.dispose_routed_slot_group(route_group)

    models = _vision_slot_models()
    if not models:
        logger.info('[VideoStoryboard] no vision slot configured — skipping')
        return {'text': '', 'status': 'no_vision_slot', 'model': ''}

    def frame_bytes(frame: dict) -> int:
        if frame.get('bytes'):
            return int(frame['bytes'])
        try:
            return os.path.getsize(str(frame.get('path') or ''))
        except OSError:
            return 0

    avg_bytes = int(sum(frame_bytes(f) for f in frames)
                    / max(len(frames), 1))
    budget = video_frame_budget(models[0], avg_frame_bytes=avg_bytes)
    kept = _thin_frames(frames, budget)

    blocks: list[dict] = []
    for fr in kept:
        image_url = str(fr.get('url') or '')
        if not image_url and fr.get('path'):
            try:
                encoded = base64.b64encode(
                    Path(str(fr['path'])).read_bytes()).decode('ascii')
                image_url = f'data:image/jpeg;base64,{encoded}'
            except OSError as exc:
                logger.warning('[VideoStoryboard] frame read failed: %s', exc)
                continue
        if not image_url:
            continue
        blocks.append({'type': 'image_url', 'image_url': {'url': image_url}})
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
