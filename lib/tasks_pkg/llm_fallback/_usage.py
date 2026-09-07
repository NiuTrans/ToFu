"""Per-round usage emission and bounded live-record projection.

Provider usage briefly carries full-history ``_wire_*`` evidence for cache
settle, FloorRetry, and cache-break attribution. Those consumers receive the
original mapping. Public events and the task's ``apiRounds`` ledger use
``project_usage_for_round_record`` so consumed evidence does not grow once per
round for the rest of the task lifetime.
"""

from lib.cost import normalize_usage
from lib.log import get_logger
from lib.tasks_pkg.manager._events import append_event

logger = get_logger(__name__)


_EXTRA_BILLING_ROUNDS_KEY = '_extra_billing_rounds'
_COMPACT_PREFIX_FINGERPRINT_KEY = '_wire_static'


def project_usage_for_round_record(usage):
    """Return the bounded usage view retained by events and ``apiRounds``.

    Every ``_wire_*`` value except the fixed-size static-prefix fingerprint is
    attempt-local backend evidence. Cache owners read it from the unchanged
    original mapping and retain only the one previous-round copy they need;
    keeping the same growing lists in every public round record creates
    quadratic live memory and event bandwidth. Discarded billed attempts are
    emitted as their own records, so their nested carrier is removed from the
    authoring attempt as well.

    The original mapping is never mutated. Unknown public usage fields remain
    forward-compatible, and the compact prefix fingerprint stays available to
    the cost-experiment join until terminal outcome construction.
    """
    if not isinstance(usage, dict):
        return {}
    projected = {}
    for key, value in usage.items():
        if key == _EXTRA_BILLING_ROUNDS_KEY:
            continue
        if isinstance(key, str) and key.startswith('_wire_'):
            if (key == _COMPACT_PREFIX_FINGERPRINT_KEY
                    and isinstance(value, str) and len(value) <= 128):
                projected[key] = value
            continue
        projected[key] = value
    return projected


def _bounded_text(value, maximum):
    """Return one bounded diagnostic string without retaining credentials."""
    if value is None:
        return ''
    return str(value)[:maximum]


def _response_route_projection(model, usage):
    """Project the credential-free route facts for one completed response."""
    dispatch = usage.get('_dispatch') if isinstance(usage, dict) else None
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    projection = {
        # ``model`` is the logical model selected by fallback/orchestration;
        # ``resolvedModel`` is the dispatcher slot's real upstream model.
        'model': _bounded_text(model, 512),
    }
    optional_fields = (
        ('resolvedModel', dispatch.get('model'), 512),
        ('providerId', dispatch.get('provider_id'), 160),
        ('keyName', dispatch.get('key'), 160),
        # Only the already-public last four characters are retained. Never
        # project ``slot.api_key`` or another credential-bearing value here.
        ('keyTail', dispatch.get('key_tail'), 32),
    )
    for field, value, maximum in optional_fields:
        text = _bounded_text(value, maximum)
        if text:
            projection[field] = text
    return projection


def _emit_round_usage(
        task, round_num, model, usage, *, tag='', response_authoring=True):
    """Emit a per-round usage SSE event so the frontend context-health
    gauge can reflect the size of the prompt JUST sent to the model,
    without waiting for the final ``done`` event to land ``apiRounds``.

    The orchestrator's ``accumulated_usage`` is the per-message running
    sum across all rounds (see :func:`_llm_call_with_fallback`), so the
    only intra-task data that maps to "next-prompt size" is the raw
    ``usage`` dict from THIS round.  We forward exactly that, plus a
    pre-computed ``tokensIn`` (total input tokens including cache) so
    the frontend doesn't need to know the Anthropic-vs-OpenAI cache
    convention.

    Parameters
    ----------
    task : dict
        Live task dict — used by ``append_event`` for SSE delivery.
    round_num : int
        1-based round number (matches the value pushed into
        ``api_rounds``).
    model : str
        Model id actually used for this round (may differ from the
        primary on fallback / reactive paths).
    usage : dict
        Raw usage dict returned by the LLM (post-streaming).
    tag : str
        Diagnostic label such as ``R1`` / ``R3-FALLBACK`` /
        ``R5-REACTIVE``.  Logged only.
    response_authoring : bool
        Whether this call produced the assistant response for the round.
        Billed-but-discarded retries still emit their accounting event, but
        must not replace the projection used for context and serving-route
        presentation.
    """
    if not usage:
        return
    try:
        _nu = normalize_usage(usage)
        inp = _nu['input']
        cw = _nu['cache_write']
        cr = _nu['cache_read']
        # Anthropic convention: prompt_tokens excludes cache. OpenAI
        # convention: prompt_tokens already includes cache. Mirrors the
        # frontend test in ui.js:1853 / context-bar.js:_promptTokensFromUsage.
        try:
            effective_prompt = max(
                0, int(usage.get('effective_prompt_tokens') or 0))
        except (TypeError, ValueError, OverflowError):
            effective_prompt = 0
        if effective_prompt > 0:
            tokens_in = effective_prompt
        elif (cw > 0 or cr > 0) and inp <= cw + cr:
            tokens_in = inp + cw + cr
        else:
            tokens_in = inp
        out = usage.get('completion_tokens') or usage.get('output_tokens') or 0
        # Live gauge feed for the v2 turn lane (2026-08-23 "context sphere
        #   frozen during generation" root fix).  The v1 SSE lane delivered
        #   this reading straight to the context-health bar; the v2 lane
        #   reduces every raw frame to a projection fold, and ``apiRounds``
        #   only lands on the task at finalize — so mid-turn the durable
        #   projection carried NO per-round prompt size at all.  Stash a
        #   compact reading on the task BEFORE append_event: the same call
        #   folds ``_task_projection`` (lib/turn_lifecycle.py), which copies
        #   it to the turn projection as ``lastRoundUsage``.  Compact fixed
        #   shape on purpose — the raw usage dict's ``_wire_*`` diagnostics
        #   are GiB-class bloat on durable rows (2026-08-20 measurement).
        if response_authoring:
            task['_lastRoundUsage'] = {
                'round': round_num,
                **_response_route_projection(model, usage),
                'tag': _bounded_text(tag, 80),
                'tokensIn': int(tokens_in or 0),
                'tokensOut': int(out or 0),
            }
        append_event(task, {
            'type': 'round_usage',
            'roundNum': round_num,
            'model': model,
            'tag': tag,
            # Same endpoint-phase tag as the messages_snapshot (P4) — the
            # Request Inspector joins attempts per (turn, roundNum).
            'turn': task.get('_flow_phase') or '',
            'tokensIn': int(tokens_in or 0),
            'tokensOut': int(out or 0),
            'usage': project_usage_for_round_record(usage),
        })
    except Exception as e:
        logger.debug('[round_usage] emit failed (round=%s tag=%s): %s',
                     round_num, tag, e)
