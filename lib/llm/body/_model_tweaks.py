# HOT_PATH
"""Model-specific message tweaks applied before/within body construction.

Cohesive group:
  - _strip_trailing_assistant_for_claude(messages, model) -> None
  - _inject_claude_reasoning_details(messages, model) -> None
  - _inject_gemini_thought_signatures(messages, model) -> None
  - _GEMINI_SKIP_SIGNATURE (module constant)
"""

from lib.log import get_logger
from lib.model_info import is_claude, is_gemini

logger = get_logger(__name__)

# Sentinel prefix stamped on a trailing assistant turn that Claude cannot accept
# as a prefill, when it is converted to a user turn (see
# _strip_trailing_assistant_for_claude). SINGLE source of truth: cache.py keys
# on it to refuse a cache breakpoint on the converted turn (its wire bytes flip
# to a bare assistant the round it stops being the tail — caching it writes an
# entry the next round cannot read back). Changing the wording MUST stay in sync
# with lib/llm/cache.py's _is_prefill_converted().
CLAUDE_PREFILL_SENTINEL = '[Your previous response for context]:'


def _strip_trailing_assistant_for_claude(messages: list, model: str = ''):
    """Remove trailing assistant messages that trigger Claude's prefill error.

    Mutates messages in place.
    """
    if not messages:
        return

    stripped = 0
    while messages and messages[-1].get('role') == 'assistant':
        last = messages[-1]
        content = last.get('content', '') or ''
        has_tool_calls = bool(last.get('tool_calls'))

        if has_tool_calls:
            logger.warning('[Claude-prefill] Stripping trailing assistant with '
                           'tool_calls (orphaned). model=%s content=%dchars tool_calls=%d',
                           model, len(content), len(last.get('tool_calls', [])))
            messages.pop()
            stripped += 1
            continue

        if not content.strip():
            logger.debug('[Claude-prefill] Stripping trailing empty assistant. model=%s', model)
            messages.pop()
            stripped += 1
            continue

        logger.warning('[Claude-prefill] Converting trailing assistant to user context '
                       '(content=%dchars). model=%s', len(content), model)
        messages[-1] = {
            'role': 'user',
            'content': f'{CLAUDE_PREFILL_SENTINEL}\n{content}',
        }
        stripped += 1
        break

    if stripped:
        logger.info('[Claude-prefill] Fixed %d trailing assistant message(s). '
                    'model=%s final_last_role=%s',
                    stripped, model,
                    messages[-1].get('role') if messages else 'empty')


def _inject_claude_reasoning_details(messages: list, model: str) -> None:
    """Rebuild OpenRouter-style ``reasoning_details`` on replayed Claude turns.

    The sankuai OpenAI-compat gateway streams a Claude thinking block as
    ``reasoning_content`` text + a separate ``reasoning_details`` chunk
    carrying the opaque ``signature``.  On a follow-up request (Continue, or
    just the next tool-loop turn) the gateway requires that signed thinking
    block to be replayed inside ``reasoning_details`` — the flat
    ``thinking_signature`` field alone is ignored, and a thinking block with
    no signature is rejected (HTTP 400).  So whenever an assistant message
    carries BOTH ``reasoning_content`` and ``thinking_signature``, synthesise
    the ``reasoning_details`` array the gateway expects.

    Only runs for Claude (the only family using this wire shape). Mutates
    messages in-place. Idempotent: skips messages that already carry a
    populated ``reasoning_details``.
    """
    if not is_claude(model):
        return

    _patched = 0
    for msg in messages:
        if msg.get('role') != 'assistant':
            continue
        if msg.get('reasoning_details'):
            continue
        th_text = msg.get('reasoning_content') or ''
        th_sig = msg.get('thinking_signature') or ''
        if not (th_text and th_sig):
            continue
        msg['reasoning_details'] = [{
            'type': 'thinking',
            'thinking': th_text,
            'signature': th_sig,
        }]
        _patched += 1

    if _patched:
        logger.info('[build_body] Rebuilt reasoning_details (signed thinking block) '
                    'on %d replayed Claude assistant turn(s)', _patched)


# Dummy signature value recognized by Gemini to skip validation.
# Used when tool_calls originate from a non-Gemini model (cross-model fallback).
_GEMINI_SKIP_SIGNATURE = 'skip_thought_signature_validator'


def _inject_gemini_thought_signatures(messages: list, model: str) -> None:
    """Inject dummy thought_signature on tool_calls that lack one (Gemini 3.x).

    When conversation history contains tool_calls produced by a non-Gemini
    model (e.g. Claude) and dispatch falls back to Gemini, the API rejects
    with HTTP 400 because those tool_calls have no thought_signature.

    Per Google's docs, setting the signature to
    ``'skip_thought_signature_validator'`` skips validation for injected/
    cross-model tool_calls.

    Mutates messages in-place. Only runs for Gemini models.
    """
    if not is_gemini(model):
        return

    _patched = 0
    for msg in messages:
        if msg.get('role') != 'assistant':
            continue
        tool_calls = msg.get('tool_calls')
        if not tool_calls:
            continue
        # Gemini requires thought_signature on the first tool_call per step.
        first_tc = tool_calls[0]
        ec = first_tc.get('extra_content')
        if ec and isinstance(ec, dict):
            google = ec.get('google')
            if isinstance(google, dict) and google.get('thought_signature'):
                continue  # already has a real signature
        # Missing — inject the skip sentinel
        if not isinstance(ec, dict):
            ec = {}
            first_tc['extra_content'] = ec
        google = ec.get('google')
        if not isinstance(google, dict):
            google = {}
            ec['google'] = google
        google['thought_signature'] = _GEMINI_SKIP_SIGNATURE
        _patched += 1

    if _patched:
        logger.info('[build_body] Injected dummy thought_signature on %d '
                    'assistant tool_call message(s) for Gemini cross-model '
                    'compatibility', _patched)
