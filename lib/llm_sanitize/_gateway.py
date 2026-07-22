"""lib/llm_sanitize/_gateway.py — Gateway keyword sanitization.

Replaces corporate-gateway-blocked keywords with semantically-equivalent
alternatives, and applies that replacement across a whole message list.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Gateway keyword sanitization
# ══════════════════════════════════════════════════════════
#
# The corporate gateway (your-llm-gateway.example.com) applies keyword-level content
# filters that block entire requests when specific strings appear in the
# prompt — even in benign contexts (e.g. news headlines, economic reports).
# These are gateway-level blocks (HTTP 450) that cannot be bypassed.
#
# The filter is key-specific (key_1 only) but since dispatch rotates keys,
# any request containing blocked terms will intermittently fail.
#
# Strategy: replace blocked exact strings with semantically-equivalent
# alternatives that the LLM understands identically.
#
# NOTE (2026-07-20): the values below are currently IDENTITY placeholders —
# each term maps to itself, so the sanitizer is INERT (it changes nothing).
# Supplying real replacement values is owner-gated: (1) choosing them is a
# content/policy call (a euphemism, or an invisible-separator technique), and
# (2) their efficacy can only be verified against the live corporate gateway
# (your-llm-gateway.example.com), which is not reachable from CI/dev. Identity entries are
# skipped in _sanitize_gateway_content so they never emit a false "Replaced"
# log line. Replace a value with a genuine alternative to activate a term.
#
# Discovered via binary search probing (2026-04-03):
_GATEWAY_BLOCKED_TERMS = {
    '习主席':  '习主席',     # Xi Jinping / General Secretary Xi → Chairman Xi
    '江主席':  '江主席',     # Jiang Zemin → Chairman Jiang
    '赵总理':  '赵总理',     # Zhao Ziyang → Premier Zhao
    'FLG':  'FLG',       # Falun Gong / Falun Dafa → abbreviation
    'QNS':  'QNS',       # Eastern Lightning → abbreviation
}


def _sanitize_gateway_content(text: str) -> str:
    """Replace gateway-blocked keywords with safe equivalents.

    Applied to message content before sending to the LLM API to prevent
    HTTP 450 content filter blocks on the corporate gateway.
    Only replaces exact substring matches — no regex, no false positives.

    Returns:
        Sanitized text. If no replacements were made, returns original string.
    """
    if not text:
        return text
    replaced = []
    for blocked, safe in _GATEWAY_BLOCKED_TERMS.items():
        if blocked == safe:
            # Identity placeholder (see NOTE on _GATEWAY_BLOCKED_TERMS): a
            # no-op that must NOT be reported as a replacement. Skip it so the
            # debug log stays honest until a real value is supplied.
            continue
        if blocked in text:
            text = text.replace(blocked, safe)
            replaced.append(f'{blocked}→{safe}')
    if replaced:
        logger.debug('[Sanitize] Replaced %d gateway-blocked term(s): %s',
                     len(replaced), ', '.join(replaced))
    return text


def _sanitize_messages(messages: list) -> list:
    """Apply gateway content sanitization to all message text content.

    Handles both string content and list-of-blocks content format.
    Mutates messages in-place (called after _strip_non_api_fields which
    already returns copies).
    """
    for msg in messages:
        content = msg.get('content')
        if isinstance(content, str):
            msg['content'] = _sanitize_gateway_content(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    block['text'] = _sanitize_gateway_content(block.get('text', ''))
    return messages
