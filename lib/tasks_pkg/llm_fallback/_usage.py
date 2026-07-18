"""Per-round usage SSE emission."""

from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def _emit_round_usage(task, round_num, model, usage, *, tag=''):
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
    """
    if not usage:
        return
    try:
        inp = usage.get('prompt_tokens') or usage.get('input_tokens') or 0
        cw = (usage.get('cache_write_tokens')
              or usage.get('cache_creation_input_tokens') or 0)
        cr = (usage.get('cache_read_tokens')
              or usage.get('cache_read_input_tokens') or 0)
        # Anthropic convention: prompt_tokens excludes cache. OpenAI
        # convention: prompt_tokens already includes cache. Mirrors the
        # frontend test in ui.js:1853 / context-bar.js:_promptTokensFromUsage.
        if (cw > 0 or cr > 0) and inp <= cw + cr:
            tokens_in = inp + cw + cr
        else:
            tokens_in = inp
        out = usage.get('completion_tokens') or usage.get('output_tokens') or 0
        append_event(task, {
            'type': 'round_usage',
            'roundNum': round_num,
            'model': model,
            'tag': tag,
            'tokensIn': int(tokens_in or 0),
            'tokensOut': int(out or 0),
            'usage': dict(usage),
        })
    except Exception as e:
        logger.debug('[round_usage] emit failed (round=%s tag=%s): %s',
                     round_num, tag, e)

