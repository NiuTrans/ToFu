# HOT_PATH
"""Content-ref resolver — resolve tool_round references to actual content."""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def _resolve_content_ref(
    task: dict[str, Any],
    content_ref: dict[str, Any],
) -> str | None:
    """Resolve a ``content_ref`` to actual text from a previous tool round.

    Looks up the referenced ``tool_round`` number in ``task['toolRounds']``
    and returns the ``toolContent`` stored there.  Supports optional
    ``start``/``end`` for substring extraction.

    Parameters
    ----------
    task : dict
        Live task dict with ``toolRounds`` list.
    content_ref : dict
        Reference dict with keys: ``tool_round`` (required), ``start`` and
        ``end`` (optional character indices).

    Returns
    -------
    str or None
        The resolved content string, or ``None`` if the round was not found
        or has no toolContent.
    """
    round_num = content_ref.get('tool_round')
    if round_num is None:
        logger.warning('[content_ref] Missing tool_round in content_ref: %s', content_ref)
        return None

    for sr in task.get('toolRounds', []):
        if sr.get('roundNum') == round_num:
            content = sr.get('toolContent', '')
            if not content:
                logger.warning('[content_ref] tool_round=%d found but toolContent is empty', round_num)
                return None
            start = content_ref.get('start')
            end = content_ref.get('end')
            if start is not None or end is not None:
                total = len(content)

                def _coerce_idx(v, default):
                    if v is None:
                        return default
                    try:
                        i = int(v)
                    except (ValueError, TypeError):
                        logger.warning('[content_ref] Ignoring non-integer slice index %r '
                                       'for tool_round=%d', v, round_num)
                        return default
                    if i < 0:  # clamp negative indices to 0 (no Python-style wraparound)
                        i = 0
                    return min(i, total)

                s = _coerce_idx(start, 0)
                e = _coerce_idx(end, total)
                if e < s:
                    logger.warning('[content_ref] tool_round=%d slice end (%s) < start (%s) — '
                                   'returning empty slice', round_num, end, start)
                    e = s
                content = content[s:e]
                logger.info('[content_ref] Resolved tool_round=%d with slice [%s:%s] → %d chars',
                            round_num, s, e, len(content))
            else:
                logger.info('[content_ref] Resolved tool_round=%d → %d chars (full content)',
                            round_num, len(content))
            return content

    logger.warning('[content_ref] tool_round=%d not found in %d toolRounds',
                   round_num, len(task.get('toolRounds', [])))
    return None
