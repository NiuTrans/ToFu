"""Per-task estimator calibration against provider ground truth.

The compaction gate counts tokens with estimate tiers (tiktoken / offline
tokenizers / CJK-aware heuristic) whose systematic bias is proven in the
incident record: tiktoken reads ~0.66x of the gateway on Claude traffic,
the entropy heuristic reads up to ~10x on CJK-heavy content
(``_constants.py`` floor notes). The heuristic floor guards the safety
direction but cannot fix the *scale* error — it only clamps one estimate
against another.

Every successful provider stream, however, reports the exact billed input
size in its ``usage`` block. This module closes the loop:

1. ``note_stream_ground_truth`` — the stream layer records the last real
   ``prompt_tokens`` (plus a monotonic sequence) on the task.
2. ``update_and_stash_sample`` — the gate pairs the PREVIOUS round's raw
   estimate with the real usage that immediately followed it (the root
   loop is strictly sequential: gate N → stream N → gate N+1), folds the
   observed ``real / estimate`` ratio into a clamped EWMA per
   ``(model, estimator-tier)``, then stashes this round's raw estimate as
   the next sample.
3. ``calibrated_estimate`` — estimate-tier gate counts are multiplied by
   the stored ratio. Exact tiers (``usage_cache`` / provider count APIs)
   never need it; when calibration was applied the heuristic floor is
   skipped, because ground-truth scaling supersedes estimate-vs-estimate
   clamping.

Failure discipline: calibration is task-local, never persisted, never
raises, and degrades to "no calibration" — the pre-existing floor logic
then applies unchanged. All ratio updates are clamped to
``[_RATIO_MIN, _RATIO_MAX]`` so a single pathological round cannot swing
the gate, and samples below ``_MIN_SAMPLE_TOKENS`` are ignored because
small-context ratios are noise-dominated.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# Tiers whose counts are provider-MEASURED; they outrank calibration and
# are never scaled (same set as ``_tokens._count_tokens_authoritative``).
EXACT_TIERS = frozenset({'usage_cache', 'anthropic_api', 'gemini_api'})

_MIN_SAMPLE_TOKENS = 2_000
_RATIO_MIN = 0.25  # covers the measured ~10x CJK heuristic over-count
_RATIO_MAX = 3.0   # covers the measured 0.66x tiktoken under-count (~1.5x)
_EWMA_ALPHA = 0.5

_TASK_KEY = '_tokenCalibration'
_SAMPLE_KEY = '_gateCalibrationSample'
_STREAM_KEY = '_lastStreamGroundTruth'

# How long one provider measurement may bound the next prompt. A measurement is
# a snapshot of a prompt that actually existed; the message list only grows
# within a turn, but an idle conversation can have been rewritten by compaction
# or lost its tool-schema identity. One hour matches the token counter's own
# usage-cache TTL and comfortably spans one agent turn's rounds.
_ANCHOR_MAX_AGE_SECONDS = 3600.0
# Guard against an implausible message-count fact turning the suffix slice
# itself into the expensive operation.
_MAX_ANCHOR_MESSAGES = 200_000


def _normalize_tier(method: str) -> str:
    """Map a gate method string to its estimator channel.

    Suffixes (``+heuristic_floor`` / ``+cal``) are stripped and the two
    heuristic spellings (resolver tier vs gate fallback path) share one
    channel so a resolver degradation does not strand the learned ratio.
    """
    base = str(method or '').split('+', 1)[0]
    if base == 'heuristic_fallback':
        return 'heuristic'
    return base


def note_stream_ground_truth(
        task: dict[str, Any] | None, *,
        model: str, prompt_tokens: int,
        message_count: int | None = None,
        now: float | None = None) -> None:
    """Record one provider-measured input size on the task.

    Called once per successful stream settlement. Overwrites the previous
    record; the monotonic ``seq`` lets the gate detect a failed round in
    which no usage arrived (stale pairing is worse than no pairing).

    ``message_count`` is the length of the request that produced this usage.
    It positions the measurement inside a later message list so
    :func:`provider_prompt_anchor` can bound appended growth; without it the
    record still feeds ratio learning but cannot anchor a prompt.
    """
    if not isinstance(task, dict):
        return
    try:
        tokens = int(prompt_tokens)
    except (TypeError, ValueError, OverflowError):
        return
    if tokens <= 0:
        return
    record = {
        'prompt_tokens': tokens,
        'model': str(model or ''),
        'seq': int(task.get('_streamGroundTruthSeq') or 0) + 1,
        'measured_at': time.time() if now is None else float(now),
    }
    if (type(message_count) is int and 0 < message_count <= _MAX_ANCHOR_MESSAGES):
        record['message_count'] = message_count
    task[_STREAM_KEY] = record
    task['_streamGroundTruthSeq'] = record['seq']


def provider_prompt_anchor(
        task: dict[str, Any] | None, *,
        model: str, messages: list | None,
        now: float | None = None) -> int | None:
    """Upper bound on the CURRENT prompt from the last provider measurement.

    A provider's billed input is measured evidence; a local estimate is not.
    The message list of one conversation only grows, so what the provider
    actually received bounds the prompt it will receive next, plus the cost of
    messages appended since. That makes this a snapshot bound
    (``measured + estimate(appended suffix)``), not an unbounded historical
    yardstick: it can never report a prompt smaller than one the provider
    already accepted.

    Returns ``None`` unless the bound is sound:

    * a measurement exists for this exact model — another model's wire
      encoding makes its size incomparable;
    * the measurement recorded the request's message count, so the suffix
      position is known;
    * the current list still starts with at least that many messages — a
      rewritten/shorter list is no longer described by the measurement;
    * the measurement is recent (``_ANCHOR_MAX_AGE_SECONDS``).

    The caller must treat the result as DOWN-ONLY: it may lower an
    estimate-tier count, never raise one.
    """
    if not isinstance(task, dict) or not isinstance(messages, list) or not messages:
        return None
    truth = task.get(_STREAM_KEY)
    if not isinstance(truth, dict) or truth.get('model') != str(model or ''):
        return None
    measured = truth.get('prompt_tokens')
    count = truth.get('message_count')
    if (type(measured) is not int or measured <= 0
            or type(count) is not int or count <= 0 or count > len(messages)):
        return None
    measured_at = truth.get('measured_at')
    if not isinstance(measured_at, (int, float)):
        return None
    age = (time.time() if now is None else float(now)) - float(measured_at)
    if age < 0 or age > _ANCHOR_MAX_AGE_SECONDS:
        return None
    try:
        from lib.token_counter.base import (
            _IMAGE_TOKENS_DEFAULT, count_images, iter_message_texts,
        )
        from lib.token_counter.heuristic import cheap_estimate_text

        suffix = messages[count:]
        appended = sum(cheap_estimate_text(text)
                       for text in iter_message_texts(suffix))
        appended += count_images(suffix) * _IMAGE_TOKENS_DEFAULT
        return int(measured) + int(appended)
    except Exception as exc:  # advisory bound — never disturb the gate
        logger.debug('[Compact] provider prompt anchor unavailable: %s', exc)
        return None


def calibration_ratio(
        task: dict[str, Any] | None, *,
        model: str, method: str) -> float | None:
    """Return the learned ratio for this (model, estimator-tier), if any."""
    if not isinstance(task, dict):
        return None
    cal = task.get(_TASK_KEY)
    if not isinstance(cal, dict) or cal.get('model') != str(model or ''):
        return None
    ratio = (cal.get('ratios') or {}).get(_normalize_tier(method))
    if not isinstance(ratio, (int, float)) or not (0 < ratio <= 10):
        return None
    return float(ratio)


def consume_stream_sample(task, *, model):
    """Learn from the final dispatched sample before measuring the next prompt.

    Recounts within one dispatch must not consume an unobserved sample or
    learn twice from one stream. Fallback/model mismatch stays uncalibrated.
    """
    if not isinstance(task, dict):
        return
    sample, truth = task.get(_SAMPLE_KEY), task.get(_STREAM_KEY)
    if not isinstance(sample, dict) or not isinstance(truth, dict):
        return
    sampled, observed = sample.get('usage_seq'), truth.get('seq')
    if type(sampled) is int and type(observed) is int and observed > sampled:
        update_and_stash_sample(task, model=model, raw_estimate=0, method='unknown')
        task.pop(_SAMPLE_KEY, None)


def discard_dispatch_sample(task):
    """A separately rebuilt reactive request cannot use the original sample."""
    if isinstance(task, dict):
        task.pop(_SAMPLE_KEY, None)


def calibrated_estimate(
        task: dict[str, Any] | None, *,
        model: str, method: str, estimate: int) -> tuple[int, bool]:
    """Scale an estimate-tier count by the learned ratio, when known.

    Returns ``(tokens, applied)``. Exact tiers and unknown ratios pass
    through unchanged so callers can keep their existing fallback logic
    for the un-calibrated case.
    """
    tier = _normalize_tier(method)
    if tier in EXACT_TIERS:
        return int(estimate), False
    ratio = calibration_ratio(task, model=model, method=method)
    if ratio is None:
        return int(estimate), False
    return max(1, int(int(estimate) * ratio)), True


def update_and_stash_sample(
        task: dict[str, Any], *,
        model: str, raw_estimate: int, method: str) -> None:
    """Pair the previous gate sample with its observed real usage, then
    stash this round's raw estimate as the next sample.

    Pairing rules (any violation simply skips the update):

    - the previous sample, the ground truth, and this call must agree on
      the model;
    - the ground truth must be from the stream IMMEDIATELY following the
      sampled gate (``seq == sample.seq + 1``) — a failed stream in
      between leaves the pairing stale and is dropped;
    - the sample must come from an estimate tier and be large enough for
      the ratio to be meaningful.
    """
    model = str(model or '')
    sample = task.pop(_SAMPLE_KEY, None)
    try:
        truth = task.get(_STREAM_KEY)
        if isinstance(sample, dict) and isinstance(truth, dict):
            sample_tier = _normalize_tier(sample.get('method'))
            raw = int(sample.get('raw_estimate') or 0)
            real = int(truth.get('prompt_tokens') or 0)
            # NB: seq values are legitimately 0 — never use ``or`` defaults.
            _truth_seq = truth.get('seq')
            _sample_seq = sample.get('usage_seq')
            _seq_ok = (
                isinstance(_truth_seq, int)
                and isinstance(_sample_seq, int)
                and _truth_seq == _sample_seq + 1)
            if (sample_tier not in EXACT_TIERS
                    and raw >= _MIN_SAMPLE_TOKENS and real > 0
                    and sample.get('model') == model
                    and truth.get('model') == model
                    and _seq_ok):
                ratio = min(_RATIO_MAX, max(_RATIO_MIN, real / raw))
                cal = task.get(_TASK_KEY)
                if not isinstance(cal, dict) or cal.get('model') != model:
                    cal = {'model': model, 'ratios': {}}
                ratios = cal.setdefault('ratios', {})
                prev = ratios.get(sample_tier)
                if isinstance(prev, (int, float)) and 0 < prev <= 10:
                    ratio = _EWMA_ALPHA * float(prev) + (1 - _EWMA_ALPHA) * ratio
                ratios[sample_tier] = ratio
                cal['updatedAt'] = time.time()
                task[_TASK_KEY] = cal
                logger.info(
                    '[Compact] token calibration conv-model=%s tier=%s '
                    'real=%d raw=%d ratio=%.3f (prev=%s)',
                    model, sample_tier, real, raw, ratio,
                    f'{prev:.3f}' if isinstance(prev, (int, float)) else '—')
    except Exception as exc:  # advisory only — never disturb the gate
        logger.debug('[Compact] token calibration update skipped: %s', exc)
    task[_SAMPLE_KEY] = {
        'raw_estimate': int(raw_estimate or 0),
        'method': str(method or ''),
        'model': model,
        'usage_seq': int(task.get('_streamGroundTruthSeq') or 0),
    }


__all__ = [
    'EXACT_TIERS',
    'calibrated_estimate',
    'calibration_ratio',
    'consume_stream_sample',
    'discard_dispatch_sample',
    'note_stream_ground_truth',
    'provider_prompt_anchor',
    'update_and_stash_sample',
]
