"""routes/chat_human_io.py — Stdin and human-guidance response endpoints.

Extracted from ``routes/chat.py``. Both endpoints resolve a pending
request that a running task is blocked on (subprocess stdin / human
guidance prompt). They share no state with the rest of ``chat.py``
beyond the public ``chat_bp`` Blueprint.
"""


from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok
from lib.human_gate_contract import (
    MAX_HUMAN_GATE_REQUEST_ID_LENGTH,
    MAX_HUMAN_GATE_RESPONSE_LENGTH,
)
from lib.log import get_logger
from lib.request_parser import optional_bool, optional_str, parse_body
from routes.api_v1.chat import api_v1_chat_bp  # noqa: E402
from routes.api_v1.auth import request_user_id, require_scope

logger = get_logger(__name__)


@api_v1_chat_bp.route('/api/v1/chat/stdin-response', methods=['POST'], endpoint='ui_chat_stdin_response')
@require_scope('chat')
def chat_stdin_response():
    """Provide stdin input to a subprocess waiting for user input.

    Body: { "stdinId": "stdin_...", "input": "user's text", "eof": false }
    If ``eof`` is true, stdin is closed (no input is sent).
    """
    data = parse_body()
    stdin_id = optional_str(
        data, 'stdinId', default='',
        max_len=MAX_HUMAN_GATE_REQUEST_ID_LENGTH)
    is_eof = optional_bool(data, 'eof', default=False)
    input_text = optional_str(
        data, 'input', default='', strip=False,
        max_len=MAX_HUMAN_GATE_RESPONSE_LENGTH)
    logger.info('[Stdin] /api/chat/stdin_response received: '
                'stdinId=%s, eof=%s, input_len=%d',
                stdin_id, is_eof, len(input_text))
    if not stdin_id:
        logger.warning('[Stdin] Rejected — missing stdinId')
        return api_bad_request('No stdinId')

    from lib.tasks_pkg.stdin_handler import resolve_stdin
    # EOF → resolve with None to signal stdin close
    resolved_text = None if is_eof else input_text
    try:
        ok = resolve_stdin(
            stdin_id,
            resolved_text,
            owner_user_id=request_user_id(),
        )
    except Exception as e:
        logger.error('[Stdin] Exception resolving %s: %s',
                     stdin_id, e, exc_info=True)
        return api_internal_error('Internal server error')
    if not ok:
        logger.warning('[Stdin] Request not found or expired: stdinId=%s',
                       stdin_id)
        return api_not_found('Stdin request not found or expired')
    logger.info('[Stdin] Successfully resolved %s', stdin_id)
    return api_ok({'stdinId': stdin_id})
@api_v1_chat_bp.route('/api/v1/chat/human-response', methods=['POST'], endpoint='ui_chat_human_response')
@require_scope('chat')
def chat_human_response():
    """Resolve a human guidance request — the user has answered a question.

    Body: { "guidanceId": "hg_...", "response": "user's answer text" }
    """
    data = parse_body()
    guidance_id = optional_str(
        data, 'guidanceId', default='',
        max_len=MAX_HUMAN_GATE_REQUEST_ID_LENGTH)
    response_text = optional_str(
        data, 'response', default='', strip=False,
        max_len=MAX_HUMAN_GATE_RESPONSE_LENGTH)
    logger.info('[HumanGuidance] /api/chat/human_response received: '
                'guidanceId=%s, response_len=%d',
                guidance_id, len(response_text))
    if not guidance_id:
        logger.warning('[HumanGuidance] Rejected — missing guidanceId')
        return api_bad_request('No guidanceId')
    if not response_text.strip():
        logger.warning('[HumanGuidance] Rejected — empty response for '
                       'guidanceId=%s', guidance_id)
        return api_bad_request('No response text')

    from lib.tasks_pkg.human_guidance import resolve_human_guidance
    try:
        ok = resolve_human_guidance(
            guidance_id,
            response_text,
            owner_user_id=request_user_id(),
        )
    except Exception as e:
        logger.error('[HumanGuidance] Exception resolving %s: %s',
                     guidance_id, e, exc_info=True)
        return api_internal_error('Internal server error')
    if not ok:
        logger.warning('[HumanGuidance] Guidance request not found or '
                       'expired: guidanceId=%s', guidance_id)
        return api_not_found('Guidance request not found or expired')
    logger.info('[HumanGuidance] Successfully resolved %s (response_len=%d)',
                guidance_id, len(response_text))
    return api_ok({'guidanceId': guidance_id})
