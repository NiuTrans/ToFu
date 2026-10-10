"""Wire-byte estimation — independent safety metric for reactive compaction.

The gateway's HTTP 413 cap measures serialized body bytes, not tokens, so
``_estimate_wire_bytes`` gives reactive_compact a metric orthogonal to the
upstream token count.
"""

import json

from lib.log import get_logger

logger = get_logger(__name__)


def _estimate_wire_bytes(messages: list) -> int:
    """Rough estimate of the serialized JSON body size (UTF-8 bytes).

    Used as an independent safety metric orthogonal to upstream token
    count, because the gateway's HTTP 413 cap measures bytes, not tokens.
    """
    try:
        return len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
    except Exception as e:
        logger.debug('[WireSize] json.dumps failed (%s) — falling back to char estimate', e)
        total = 0
        for m in messages:
            try:
                total += len(str(m))
            except Exception as e:
                logger.debug('[WireSize] str(message) failed: %s', e)
        return total
