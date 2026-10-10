"""Renderer-neutral program timeline and transition-handle contract.

An overlap transition must not silently shorten a narrated film.  This module
uses the same model as a non-linear editor: the spoken/content duration is the
program time, while the outgoing scene receives an extra visual *handle*.
FFmpeg overlaps that handle with the next scene, so the final duration remains
exactly the sum of content durations and the narration/SRT clock does not move.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'TIMELINE_CONTRACT_VERSION', 'TRANSITION_SPECS',
    'normalise_timeline_contract', 'timeline_contract_errors',
    'transition_plan',
]

TIMELINE_CONTRACT_VERSION = 'motion-timeline-v1'

# ``ffmpeg`` names are from the xfade filter.  A match cut is authored in the
# shot choreography and remains a zero-duration edit here.
TRANSITION_SPECS: dict[str, dict] = {
    'cold-open': {'overlap_s': 0.0, 'ffmpeg': ''},
    'cut': {'overlap_s': 0.0, 'ffmpeg': ''},
    'match-cut': {'overlap_s': 0.0, 'ffmpeg': ''},
    'push': {'overlap_s': 0.32, 'ffmpeg': 'slideleft'},
    'wipe': {'overlap_s': 0.28, 'ffmpeg': 'wipeleft'},
    'dissolve': {'overlap_s': 0.40, 'ffmpeg': 'fade'},
}


def _seconds(value, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        logger.debug('[MotionVideo] invalid timeline seconds %r: %s', value, exc)
        return float(fallback)
    return parsed if parsed >= 0 else float(fallback)


def _content_duration(scene: dict, durations: dict[str, float]) -> float:
    scene_id = str(scene.get('id') or '')
    if scene_id in durations:
        return max(0.001, _seconds(durations[scene_id], 0.001))
    explicit = scene.get('content_duration_s')
    if explicit is not None:
        return max(0.001, _seconds(explicit, 0.001))
    return max(0.001, _seconds(scene.get('end'), 0)
               - _seconds(scene.get('start'), 0))


def normalise_timeline_contract(
        scenes: list[dict], *, durations: dict[str, float] | None = None,
        fps: float = 30.0) -> dict:
    """Attach program-time and render-handle metadata to ``scenes``.

    ``transition_in`` lives on the incoming scene.  For a 0.4s dissolve into
    scene B, scene A renders an additional 0.4s resolved-state handle.  The
    transition starts at B's program start, so the final program duration is
    unchanged and B's narration can begin on its original clock.
    """
    if not isinstance(scenes, list) or not scenes:
        return {'version': TIMELINE_CONTRACT_VERSION, 'duration_s': 0.0,
                'fps': float(fps), 'transitions': []}
    duration_map = durations or {}
    fps = max(1.0, _seconds(fps, 30.0))
    cursor = 0.0
    for index, scene in enumerate(scenes):
        content = _content_duration(scene, duration_map)
        kind = str(scene.get('transition_in') or
                   ('cold-open' if index == 0 else 'cut')).strip().lower()
        if kind not in TRANSITION_SPECS:
            kind = 'cold-open' if index == 0 else 'cut'
        if index == 0:
            kind = 'cold-open'
        spec = TRANSITION_SPECS[kind]
        requested = _seconds(scene.get('transition_duration_s'),
                             spec['overlap_s'])
        # Keep a transition subordinate to the incoming shot.  One frame is
        # the smallest meaningful non-zero overlap; 25% / 0.8s is the ceiling.
        max_overlap = min(0.8, content * 0.25)
        overlap = min(requested, max_overlap) if spec['ffmpeg'] else 0.0
        if 0 < overlap < 1.0 / fps:
            overlap = 1.0 / fps
        overlap = round(overlap, 4)
        start = round(cursor, 4)
        end = round(start + content, 4)
        scene.update({
            'timeline_contract_version': TIMELINE_CONTRACT_VERSION,
            'content_duration_s': round(content, 4),
            'timeline_start_s': start,
            'timeline_end_s': end,
            'transition_in': kind,
            'transition_in_duration_s': overlap,
            'transition_ffmpeg': spec['ffmpeg'] if overlap else '',
            'transition_window_s': ([start, round(start + overlap, 4)]
                                    if overlap else []),
        })
        cursor = end

    # The incoming overlap is rendered as an outgoing handle on its previous
    # scene.  This is the invariant that preserves program duration.
    for index, scene in enumerate(scenes):
        outgoing = (float(scenes[index + 1]['transition_in_duration_s'])
                    if index + 1 < len(scenes) else 0.0)
        render_duration = float(scene['content_duration_s']) + outgoing
        scene['outgoing_handle_s'] = round(outgoing, 4)
        scene['render_duration_s'] = round(render_duration, 4)

    return {
        'version': TIMELINE_CONTRACT_VERSION,
        'duration_s': round(cursor, 4),
        'fps': float(fps),
        'transitions': transition_plan(scenes),
    }


def transition_plan(scenes: list[dict]) -> list[dict]:
    """Return the ordered N-1 transition plan consumed by assembly."""
    return [
        {
            'scene_id': str(scene.get('id') or ''),
            'kind': str(scene.get('transition_in') or 'cut'),
            'duration_s': round(float(
                scene.get('transition_in_duration_s') or 0), 4),
            'ffmpeg': str(scene.get('transition_ffmpeg') or ''),
        }
        for scene in (scenes or [])[1:]
    ]


def timeline_contract_errors(scenes: list[dict]) -> list[str]:
    """Validate the persisted program/render relationship."""
    if not isinstance(scenes, list) or not scenes:
        return ['timeline must be a non-empty scene list']
    errors: list[str] = []
    cursor = 0.0
    for index, scene in enumerate(scenes):
        label = str(scene.get('id') or f'#{index + 1}')
        if scene.get('timeline_contract_version') != TIMELINE_CONTRACT_VERSION:
            errors.append(f'scene {label}: missing timeline contract version')
            continue
        content = _seconds(scene.get('content_duration_s'), -1)
        handle = _seconds(scene.get('outgoing_handle_s'), -1)
        render = _seconds(scene.get('render_duration_s'), -1)
        start = _seconds(scene.get('timeline_start_s'), -1)
        end = _seconds(scene.get('timeline_end_s'), -1)
        if content <= 0:
            errors.append(f'scene {label}: content duration must be positive')
        if abs(start - cursor) > 0.01 or abs(end - (start + content)) > 0.01:
            errors.append(f'scene {label}: program timeline is not contiguous')
        if abs(render - (content + handle)) > 0.01:
            errors.append(f'scene {label}: render duration does not include handle')
        if index + 1 < len(scenes):
            expected = _seconds(
                scenes[index + 1].get('transition_in_duration_s'), 0)
            if abs(handle - expected) > 0.01:
                errors.append(f'scene {label}: outgoing handle/transition drift')
        elif handle > 0.001:
            errors.append(f'scene {label}: final scene cannot have an outgoing handle')
        cursor = end
    return errors
