"""lib/motion_video/_storyboard.py — Zero-LLM storyboard fallback.

The headless engine path can't ask a model to split scenes, so this greedy
segmenter produces a VALID storyboard by construction:

  * cues accumulate into the current scene while the scene is short;
  * a scene closes when it reaches ``target`` seconds, preferring a
    sentence-final cue boundary (。！？!?.…); it MUST close at ``max``;
  * inter-cue silence folds into the PREVIOUS scene (same rule as the
    agent workflow) — scenes are contiguous by construction;
  * the result always spans the full SRT range and passes
    :func:`lib.motion_video._gates.check_storyboard`.

The chat-agent path stays smarter (semantic splits); this is the
deterministic floor the engine and tests can rely on.
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_storyboard']

_SENTENCE_FINAL_RE = re.compile(r'[。！？!?…]+$|(?:\.|\?|!)\s*$')

_DEFAULT_MIN = 2.0
_DEFAULT_TARGET = 6.0
_DEFAULT_MAX = 12.0


def build_storyboard(entries, *, min_scene: float = _DEFAULT_MIN,
                     target_scene: float = _DEFAULT_TARGET,
                     max_scene: float = _DEFAULT_MAX) -> list[dict]:
    """Greedily segment SRT entries into scenes.

    Args:
        entries: list of :class:`lib.motion_video._srt.SrtEntry` (sorted).
        min_scene: never close a scene shorter than this (unless it's the last).
        target_scene: preferred close point — close at the first
            sentence-final cue end ≥ target.
        max_scene: hard close at this duration even mid-sentence.

    Returns a scenes list (id/start/end/text/visual) ready for
    ``check_storyboard`` — contiguous, full-coverage, by construction.
    """
    if not entries:
        return []
    if min_scene <= 0 or target_scene < min_scene or max_scene < target_scene:
        logger.warning('[MotionVideo] storyboard bounds odd (min=%s target=%s '
                       'max=%s) — clamping', min_scene, target_scene, max_scene)
        min_scene = max(0.5, min_scene)
        target_scene = max(min_scene, target_scene)
        max_scene = max(target_scene, max_scene)

    scenes: list[dict] = []
    cur_start = entries[0].start
    cur_texts: list[str] = []
    cur_end = entries[0].start

    def _close(at_end: float) -> None:
        nonlocal cur_texts
        text = ' '.join(t for t in cur_texts if t).strip()
        scenes.append({
            'id': f'scene-{len(scenes) + 1:03d}',
            'start': round(cur_start, 3),
            'end': round(at_end, 3),
            'text': text,
            'visual': '',
        })
        cur_texts = []

    for e in entries:
        cur_texts.append(e.text)
        cur_end = e.end
        dur = cur_end - cur_start
        if dur >= max_scene:
            _close(cur_end)
            cur_start = cur_end
        elif dur >= target_scene and _SENTENCE_FINAL_RE.search(e.text or ''):
            _close(cur_end)
            cur_start = cur_end
        # else keep accumulating (min/target not reached)
    if cur_texts:
        _close(cur_end)

    # Merge a trailing runt (< min_scene) into its predecessor when possible
    # — but never at the price of violating the max_scene contract.
    if len(scenes) > 1 and scenes[-1]['end'] - scenes[-1]['start'] < min_scene:
        if scenes[-1]['end'] - scenes[-2]['start'] <= max_scene:
            runt = scenes.pop()
            scenes[-1]['end'] = runt['end']
            scenes[-1]['text'] = (scenes[-1]['text'] + ' ' + runt['text']).strip()

    logger.info('[MotionVideo] zero-LLM storyboard: %d cue(s) → %d scene(s), '
                'span %.2fs', len(entries), len(scenes),
                scenes[-1]['end'] - scenes[0]['start'])
    return scenes
