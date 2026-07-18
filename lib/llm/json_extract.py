"""Shared LLM-reply JSON extraction.

The canonical home for "pull the first JSON object out of an LLM reply,
tolerating a ```json code fence or leading prose". This logic was independently
duplicated byte-for-byte (modulo the log prefix) in the paper recommend and
insight engines — hoisted here so both call sites share one implementation.

The extraction is deliberately forgiving in exactly one way — it strips a
leading fence OR skips to the first ``{`` — and otherwise defers to
``json.loads``. Callers pass their own ``log_prefix`` + ``logger`` so the
warning line still identifies the feature that produced the unparseable reply.
"""

from __future__ import annotations

import json
import re

from lib.log import get_logger

logger = get_logger(__name__)

_FENCE_RE = re.compile(r'```(?:json)?\s*(\{.*\})\s*```', re.DOTALL)


def extract_first_json_object(content, *, log_prefix='[LLM]', log=None):
    """Extract the first JSON object from an LLM reply (tolerates code fences).

    Args:
        content: the raw LLM reply text (or falsy → returns None).
        log_prefix: prefix for the "not parseable" warning, so the log line
            still names the calling feature (e.g. ``'[Paper:Insight]'``).
        log: optional logger to warn through; defaults to this module's logger.

    Returns:
        The parsed object (usually a dict), or ``None`` when the reply is empty
        or not parseable as JSON.
    """
    if not content:
        return None
    text = content.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    else:
        brace = text.find('{')
        if brace > 0:
            text = text[brace:]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        (log or logger).warning('%s LLM reply was not parseable JSON: %s', log_prefix, e)
        return None
