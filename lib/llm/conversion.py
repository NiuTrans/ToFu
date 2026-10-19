"""Local protocol conversion checks; no provider I/O or persisted state.

Errors contain fixed codes only. Reports count optional portable reasoning losses.
"""
import hashlib
import json
import math

from lib.llm.llm_errors import LocalRequestPreparationError

from dataclasses import dataclass, field
from copy import deepcopy


@dataclass(frozen=True)
class ConversionResult:
    """Checked conversion: no payload on failure; fixed-code diagnostics only."""

    payload: dict | None
    validation_issues: tuple[str, ...] = ()
    permitted_conversion_losses: dict[str, int] = field(default_factory=dict)
    tool_name_reverse: dict[str, str] = field(default_factory=dict)

    def require_payload(self):
        if self.validation_issues:
            raise ProtocolConversionError(self.validation_issues[0])
        return self.payload


def checked_conversion(convert, body, **kwargs):
    """Run focused adapters on an isolated snapshot, retaining bounded losses."""
    try:
        converted = convert(deepcopy(body), **kwargs)
        payload, reverse = converted if isinstance(converted, tuple) else (converted, {})
        return ConversionResult(payload, (), dict(payload.get('_conversion_report', {})), reverse)
    except ProtocolConversionError as exc:
        return ConversionResult(None, (exc.code,))


class ProtocolConversionError(LocalRequestPreparationError, ValueError):
    """Invalid local request, neutral to upstream health like other ValueErrors."""

    def __init__(self, code):
        self.code = code
        super().__init__('protocol_conversion:' + code)


def _invalid_constant(_value):
    raise ValueError('nonfinite_json')


# Immutable decoder witnesses keep injected/custom decoders on the encoder
# validation boundary. No caller data is retained.
_ARGUMENT_JSON_LOADS = json.loads
_ARGUMENT_JSON_DECODER = json.JSONDecoder
_ARGUMENT_JSON_DECODE = json.JSONDecoder.decode
_ARGUMENT_JSON_RAW_DECODE = json.JSONDecoder.raw_decode


def object_arguments(value):
    """Decode once; preserve shape, numeric and custom-carrier failure policy."""
    if not isinstance(value, (str, dict)):
        raise ProtocolConversionError('tool_arguments_object_required')
    trusted = False
    nonfinite = False
    if isinstance(value, str):
        encoded = value
        trusted = (json.loads is _ARGUMENT_JSON_LOADS
                   and json.JSONDecoder is _ARGUMENT_JSON_DECODER
                   and json.JSONDecoder.decode is _ARGUMENT_JSON_DECODE
                   and json.JSONDecoder.raw_decode is _ARGUMENT_JSON_RAW_DECODE)
        def argument_float(text):
            nonlocal nonfinite
            number = float(text)
            if not math.isfinite(number):
                nonfinite = True
            return number
        try:
            if trusted:
                try:
                    value = json.loads(encoded, parse_constant=_invalid_constant,
                                       parse_float=argument_float)
                except RecursionError:
                    # A Python float callback adds a frame near the decoder's
                    # C stack boundary. Retry the exact legacy path before
                    # declaring an input invalid.
                    trusted = False
                    value = json.loads(encoded, parse_constant=_invalid_constant)
            else:
                value = json.loads(encoded, parse_constant=_invalid_constant)
        except (ValueError, TypeError, RecursionError):
            raise ProtocolConversionError('tool_arguments_invalid_json') from None
    if not isinstance(value, dict):
        raise ProtocolConversionError('tool_arguments_object_required')
    if trusted:
        if nonfinite:
            raise ProtocolConversionError('tool_arguments_invalid_json')
        return value
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ProtocolConversionError('tool_arguments_invalid_json') from None
    return value


# These tools already have a supported native Responses representation.
RESPONSES_NATIVE_TOOLS = frozenset({
    'web_search', 'web_search_preview', 'file_search', 'computer_use_preview',
    'computer', 'code_interpreter', 'image_generation', 'local_shell', 'shell',
    'apply_patch', 'mcp', 'tool_search', 'namespace',
    'programmatic_tool_calling',
})


def validate_request(body, protocol):
    """Reject unrepresentable canonical data before translators can drop it."""
    if not isinstance(body, dict):
        raise ProtocolConversionError('request_object_required')
    tools = body.get('tools')
    if tools is None:
        tools = []
    messages = body.get('messages', [])
    if not isinstance(tools, list) or not isinstance(messages, list):
        raise ProtocolConversionError('request_list_required')
    for tool in tools:
        if not isinstance(tool, dict):
            raise ProtocolConversionError('tool_object_required')
        kind = tool.get('type')
        if not isinstance(kind, str):
            raise ProtocolConversionError('unsupported_tool')
        if kind != 'function':
            if protocol == 'responses' and kind in RESPONSES_NATIVE_TOOLS:
                continue
            raise ProtocolConversionError('unsupported_tool')
        if not isinstance(tool.get('function'), dict):
            raise ProtocolConversionError('function_object_required')
        if not isinstance(tool['function'].get('name'), str) or not tool['function']['name']:
            raise ProtocolConversionError('tool_name_invalid')
        if 'strict' in tool['function'] and not isinstance(tool['function']['strict'], bool):
            raise ProtocolConversionError('tool_strict_boolean_required')

        if 'parameters' in tool['function'] and not isinstance(tool['function']['parameters'], dict):
            raise ProtocolConversionError('tool_schema_object_required')
    if 'parallel_tool_calls' in body and not isinstance(body['parallel_tool_calls'], bool):
        raise ProtocolConversionError('parallel_tool_calls_boolean_required')
    choice = body.get('tool_choice')
    if choice is not None:
        if isinstance(choice, str):
            if choice not in {'auto', 'none', 'required', 'any'}:
                raise ProtocolConversionError('unsupported_tool_choice')
        elif isinstance(choice, dict):
            if not isinstance(choice.get('type'), str):
                raise ProtocolConversionError('unsupported_tool_choice')
            if choice.get('type') == 'function':
                fn = choice.get('function')
                if not isinstance(fn, dict) or not isinstance(fn.get('name'), str) or not fn['name']:
                    raise ProtocolConversionError('tool_name_invalid')
            elif protocol != 'anthropic' or choice.get('type') not in {'auto', 'any', 'none', 'tool'}:
                raise ProtocolConversionError('unsupported_tool_choice')

            elif choice['type'] == 'tool' and (
                    not isinstance(choice.get('name'), str) or not choice['name']):
                raise ProtocolConversionError('tool_name_invalid')
        else:
            raise ProtocolConversionError('unsupported_tool_choice')
    if 'output_config' in body and not isinstance(body['output_config'], dict):
        raise ProtocolConversionError('output_config_object_required')
    for msg in messages:
        if not isinstance(msg, dict):
            raise ProtocolConversionError('message_object_required')
        if (not isinstance(msg.get('role'), str)
                or msg['role'] not in {'system', 'developer', 'user', 'assistant', 'tool'}):
            raise ProtocolConversionError('unsupported_role')
        if msg.get('tool_calls') and msg.get('role') != 'assistant':
            raise ProtocolConversionError('tool_call_role_invalid')
        if msg.get('_anthropic_content_blocks') and msg.get('role') != 'assistant':
            raise ProtocolConversionError('native_content_role_invalid')
        if msg.get('_responses_items') and msg.get('role') != 'assistant':
            raise ProtocolConversionError('native_content_role_invalid')
        if msg.get('thinking_signature') and not isinstance(msg.get('reasoning_content'), str):
            raise ProtocolConversionError('native_content_invalid')
        if '_responses_items' in msg:
            items = msg['_responses_items']
            if not isinstance(items, list):
                raise ProtocolConversionError('native_content_list_required')
            if any(not isinstance(item, dict) or not isinstance(item.get('type'), str)
                   for item in items):
                raise ProtocolConversionError('native_content_object_required')
        native = msg.get('_anthropic_content_blocks')
        if native is not None:
            validate_native_blocks(native)
            if protocol == 'anthropic' and native:
                continue  # Original native blocks, not their lossy projection, are authority.
        content = msg.get('content')
        if content is not None and not isinstance(content, (str, list)):
            raise ProtocolConversionError('unsupported_content')
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                raise ProtocolConversionError('content_object_required')
            kind = block.get('type')
            if kind == 'text' and isinstance(block.get('text'), str):
                continue
            if kind == 'image_url' and (msg['role'] == 'user' or (
                    protocol == 'responses' and msg['role'] == 'tool') or (
                    protocol == 'anthropic' and msg['role'] in {'assistant', 'tool'})):
                image = block.get('image_url')
                if isinstance(image, dict) and isinstance(image.get('url'), str) and image['url']:
                    continue
                if protocol == 'responses' and isinstance(image, str) and image:
                    continue
            raise ProtocolConversionError('unsupported_content')
        calls = msg.get('tool_calls')
        if calls is None:
            calls = []
        if not isinstance(calls, list):
            raise ProtocolConversionError('tool_calls_list_required')
        for call in calls:
            if not isinstance(call, dict) or call.get('type', 'function') != 'function':
                raise ProtocolConversionError('unsupported_tool_call')
            fn = call.get('function')
            if not isinstance(fn, dict):
                raise ProtocolConversionError('function_object_required')
            if not isinstance(fn.get('name'), str) or not fn['name']:
                raise ProtocolConversionError('tool_name_invalid')
            object_arguments(fn.get('arguments'))


def stop_sequences(body):
    """Normalize canonical/native stop aliases without silently weakening them."""
    result = None
    for key in ('stop', 'stop_sequences'):
        value = body.get(key)
        if value is None:
            continue
        if isinstance(value, str) and key == 'stop':
            value = [value]
        if (not isinstance(value, list) or len(value) > 64
                or any(not isinstance(item, str) or not item or len(item) > 8192 for item in value)):
            raise ProtocolConversionError('stop_sequences_invalid')
        if result is not None and value != result:
            raise ProtocolConversionError('stop_sequences_conflict')
        result = list(value)
    return result


def validate_native_blocks(blocks):
    if not isinstance(blocks, list):
        raise ProtocolConversionError('native_content_list_required')
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get('type'), str):
            raise ProtocolConversionError('native_content_object_required')
        kind = block['type']
        if kind in {'tool_use', 'server_tool_use'}:
            if not isinstance(block.get('input'), dict):
                raise ProtocolConversionError('tool_arguments_object_required')
            object_arguments(block['input'])
            if not all(isinstance(block.get(k), str) and block[k] for k in ('name', 'id')):
                raise ProtocolConversionError('native_tool_invalid')
        fields = {'thinking': ('thinking', 'signature'),
                  'redacted_thinking': ('data',), 'text': ('text',)}.get(kind, ())
        if any(not isinstance(block.get(k), str) for k in fields):
            raise ProtocolConversionError('native_content_invalid')
        if kind == 'thinking' and not block['signature']:
            raise ProtocolConversionError('native_signature_required')


def conversion_report(body, protocol, *, replay_reasoning=False, replay_types=frozenset()):
    count = 0
    for msg in body.get('messages', []):
        if msg.get('role') != 'assistant' or not msg.get('reasoning_content'):
            continue
        if protocol == 'anthropic' and (msg.get('thinking_signature') or msg.get('_anthropic_content_blocks')):
            continue
        if protocol == 'responses' and replay_reasoning and any(
                isinstance(item, dict) and item.get('type') == 'reasoning'
                for item in msg.get('_responses_items') or []):
            continue
        count = min(count + 1, 2147483647)
    report = {'optional_reasoning_omitted': count} if count else {}

    def loss(code):
        report[code] = min(report.get(code, 0) + 1, 2147483647)

    for msg in body.get('messages', []):
        items = msg.get('_responses_items') or []
        if not isinstance(items, list):
            raise ProtocolConversionError('native_content_list_required')
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('type'), str):
                raise ProtocolConversionError('native_content_object_required')
            kind = item['type']
            correlation = item.get('call_id') if 'call_id' in item else item.get('id')
            if kind in {'program', 'program_output'} and not str(correlation or '').strip():
                loss('orphan_opaque_item_omitted')
                continue
            if protocol == 'responses' and replay_reasoning and kind in replay_types:
                continue

            if protocol == 'anthropic' and kind == 'reasoning':
                encrypted = item.get('encrypted_content')
                if encrypted is not None and not isinstance(encrypted, str):
                    raise ProtocolConversionError('native_content_invalid')
                if encrypted:
                    # Foreign ciphertext is not executable Messages history.
                    # Project canonical text, function calls and results without
                    # fabricating signed thinking; retain caller replay state.
                    loss('cross_protocol_reasoning_omitted')
                    continue
            if (kind == 'compaction' or kind in {'program', 'program_output'}
                    or kind.endswith('_call') or kind.endswith('_call_output')
                    or kind in {'tool_search_output', 'function_call_output'}
                    or (kind == 'reasoning' and item.get('encrypted_content'))):
                raise ProtocolConversionError('mandatory_continuity_unrepresentable')
            loss('native_metadata_omitted')
        if protocol != 'anthropic':
            if msg.get('thinking_signature'):
                if msg.get('_reasoning_continuity') != 'completed_turn':
                    raise ProtocolConversionError('mandatory_continuity_unrepresentable')
                loss('historical_thinking_signature_omitted')
            for block in msg.get('_anthropic_content_blocks') or []:
                if not isinstance(block, dict) or not isinstance(block.get('type'), str):
                    raise ProtocolConversionError('native_content_object_required')
                kind = block['type']
                if (kind in {'thinking', 'redacted_thinking'}
                        and msg.get('_reasoning_continuity') == 'completed_turn'):
                    validate_native_blocks([block])
                    loss('historical_thinking_block_omitted')
                    continue
                # Native text is projected by the Responses adapter. Every
                # remaining native block needs a cross-protocol binding;
                # unknown content is not permission to erase visible work.
                if kind != 'text':
                    raise ProtocolConversionError('mandatory_continuity_unrepresentable')
    return report


def tool_name_reverse(body):
    """Reserve all short names before assigning bounded, collision-checked aliases."""
    names = set()
    def add(name):
        if not isinstance(name, str) or not name:
            raise ProtocolConversionError('tool_name_invalid')
        names.add(name)
    for tool in body.get('tools') or []:
        if tool.get('type') == 'function':
            add(tool['function'].get('name'))
    for msg in body.get('messages', []):
        for call in msg.get('tool_calls') or []:
            add(call['function'].get('name'))
    choice = body.get('tool_choice')
    if isinstance(choice, dict) and choice.get('type') == 'function':
        add((choice.get('function') or {}).get('name'))
    names.update(str(n) for n in body.get('_frontend_selected_tool_names') or [])
    names.update(str(n) for n in body.get('_tool_namespace_by_name') or {})
    occupied = {n for n in names if len(n) <= 64}
    reverse = {}
    for name in sorted(names):
        if len(name) <= 64:
            continue
        for attempt in range(len(names) + 1):
            digest = hashlib.sha256((str(attempt) + ':' + name).encode()).hexdigest()[:24]
            alias = name[:39] + '_' + digest
            if alias not in occupied:
                occupied.add(alias)
                reverse[alias] = name
                break
        else:
            raise ProtocolConversionError('tool_name_collision')
    return reverse
