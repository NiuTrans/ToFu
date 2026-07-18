"""Rubric critic — the measurement instrument.

Scores any report on the four INSIGHT axes and returns strict JSON so one-pass
vs two-pass reports are a numeric diff, not a vibe. A single no-tool dispatch at
temp=0 (a judgement, not a creative act).

``dispatch_stream`` is resolved THROUGH the package facade at call time so a
test patching ``ie.dispatch_stream`` bites here exactly as in the flat module.
"""

from lib.agent_loop import AbortSignal
from lib.llm_errors import AbortedError
from lib.log import get_logger

from ._config import _RUBRIC_MAX_TOKENS, _RUBRIC_TEMPERATURE
from ._synthesize import _parse_llm_json
from ..insight_prompts import RUBRIC_AXES, rubric_prompt

logger = get_logger(__name__)


def _coerce_score(v):
    """Clamp a rubric score to an int in [1, 5]; None on garbage."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError) as e:
        logger.debug('[Paper:Insight] non-numeric rubric score %r (->None): %s', v, e)
        return None
    return max(1, min(5, n))


def score_report_rubric(report_md, *, model=None, abort=None):
    """Score a report on the four INSIGHT axes. Returns a parseable verdict.

    A single no-tool dispatch at temp=0 — a judgement, not a creative act. This
    is the A/B instrument: run it on the plain report AND on report+insight and
    diff ``overall`` to see whether pass 2 moved the needle.

    Returns:
        {
          'scores': {axis: 1-5, ...},   # all four axes, coerced
          'overall': float,             # mean of the four (recomputed, trusted)
          'justifications': {axis: str},
          'one_line_verdict': str,
          'raw': <parsed model json>,   # for debugging
        }
        or None on dispatch/parse failure (logged).
    """
    import lib.paper.insight_engine as _pkg
    dispatch_stream = _pkg.dispatch_stream

    report_md = (report_md or '').strip()
    if not report_md:
        logger.warning('[Paper:Insight:Rubric] Empty report — nothing to score')
        return None

    messages = [{'role': 'user', 'content': rubric_prompt(report_md)}]
    abort_signal = AbortSignal.from_callback(abort)
    buf = {'content': ''}

    def _on_content(text):
        buf['content'] += text

    try:
        msg, finish, usage = dispatch_stream(
            messages,
            on_content=_on_content,
            abort_check=abort_signal.is_set,
            prefer_model=model or None,
            strict_model=bool(model),
            capability='text',
            max_tokens=_RUBRIC_MAX_TOKENS,
            temperature=_RUBRIC_TEMPERATURE,
            thinking_enabled=False,
            log_prefix='[Paper:Insight:Rubric]',
        )
    except AbortedError:
        logger.info('[Paper:Insight:Rubric] Scoring aborted')
        return None
    except Exception as e:
        logger.error('[Paper:Insight:Rubric] Scoring dispatch failed: %s', e, exc_info=True)
        return None

    content = buf['content'] or (msg.get('content') if isinstance(msg, dict) else '') or ''
    parsed = _parse_llm_json(content)
    if not isinstance(parsed, dict):
        logger.warning('[Paper:Insight:Rubric] Unparseable rubric reply')
        return None

    raw_scores = parsed.get('scores') if isinstance(parsed.get('scores'), dict) else {}
    scores = {}
    for axis in RUBRIC_AXES:
        s = _coerce_score(raw_scores.get(axis))
        if s is not None:
            scores[axis] = s
    if not scores:
        logger.warning('[Paper:Insight:Rubric] No valid axis scores in reply')
        return None

    # Recompute the mean ourselves — never trust the model's arithmetic.
    overall = round(sum(scores.values()) / len(scores), 2)
    justifs = parsed.get('justifications') if isinstance(parsed.get('justifications'), dict) else {}
    verdict = (parsed.get('one_line_verdict') or '').strip()
    logger.info('[Paper:Insight:Rubric] scores=%s overall=%.2f', scores, overall)
    return {
        'scores': scores,
        'overall': overall,
        'justifications': {k: str(v) for k, v in justifs.items()},
        'one_line_verdict': verdict,
        'raw': parsed,
    }
