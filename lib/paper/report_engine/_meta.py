"""Build the serializable cost and usage metadata for a finished report."""

from lib.log import get_logger

logger = get_logger(__name__)


def _build_report_meta(model, provider_id, usage_total, round_count, elapsed_s):
    """Assemble the report "finish tag" metadata dict.

    Combines the resolved generation model, accumulated token usage, and the
    computed cost (via ``lib.cost.compute_cost`` — the same math the chat
    finish-info bar uses) into a small JSON-serialisable dict the frontend
    renders as a badge under the report. Cost is best-effort: a pricing miss
    leaves ``costCny``/``costUsd`` as None but the model + token counts still
    show.
    """
    cost = None
    try:
        from lib.cost import compute_cost
        cost = compute_cost(usage_total, model_id=model, provider_id=provider_id)
    except Exception as e:
        logger.warning('[Paper:Report] cost computation failed: %s', e)
    meta = {
        'model': model or '',
        'providerId': provider_id or '',
        'rounds': round_count,
        'elapsedSec': round(elapsed_s, 1),
        'promptTokens': usage_total.get('prompt_tokens', 0),
        'completionTokens': usage_total.get('completion_tokens', 0),
        'cacheReadTokens': usage_total.get('cache_read_tokens', 0),
        'cacheWriteTokens': usage_total.get('cache_write_tokens', 0),
        'costUsd': cost.get('costUsd') if cost else None,
        'costCny': cost.get('costCny') if cost else None,
    }
    return meta
