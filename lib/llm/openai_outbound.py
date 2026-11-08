"""Final Chat Completions projection shared by all HTTP entry points.

Canonical histories retain private replay carriers for other protocols. This
adapter removes those envelope fields, respells tool-call IDs the destination
would reject (``lib.llm.tool_call_ids``) and applies the existing gateway text
shape policy without modifying caller state or nested user/tool JSON.
Dependencies: common request projection and the cache-marker model policy.
"""
from lib.llm.request import strip_internal_request_fields


def openai_body_to_chat(body):
    """Project canonical input; retain public vendor reasoning extensions."""
    from lib.llm.cache import _gateway_honors_cache_markers
    from lib.llm.conversion import ProtocolConversionError, conversion_report

    model = body.get('_admission_logical_model') or body.get('model') or ''
    from lib.llm.route_features import body_route_profile
    preserve_text_parts = _gateway_honors_cache_markers(
        model, inference_profile=body_route_profile(body))
    projected = strip_internal_request_fields(body)
    messages = []
    if not isinstance(body.get('messages', []), list):
        raise ProtocolConversionError('request_list_required')
    from lib.llm.tool_call_ids import (
        respell_chat_message, tool_call_id_contract, wire_tool_call_ids,
    )
    wire_ids = wire_tool_call_ids(
        body.get('messages') or (), tool_call_id_contract('openai', model))
    for message in body.get('messages') or ():
        if not isinstance(message, dict):
            raise ProtocolConversionError('message_object_required')
        # A message without private fields is its own projection; sharing it
        # keeps a settled message's sealed encoding reusable across requests.
        clean = (strip_internal_request_fields(message)
                 if any(not isinstance(key, str) or key.startswith('_')
                        for key in message) else message)
        content = clean.get('content')
        if (not preserve_text_parts and isinstance(content, list) and content
                and all(isinstance(part, dict) and part.get('type') == 'text'
                        and isinstance(part.get('text'), str)
                        and set(part) <= {'type', 'text'} for part in content)):
            clean = {**clean, 'content': ''.join(part['text'] for part in content)}
        if wire_ids:
            clean = respell_chat_message(clean, wire_ids)
        messages.append(clean)
    projected['messages'] = messages
    report = conversion_report(body, 'openai')
    if report:
        projected['_conversion_report'] = report
    return projected
