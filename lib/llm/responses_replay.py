"""Derived Responses replay-order witnesses; no execution or storage authority.

capture_replay_order appends bounded position references to the existing private
native sidecar, never another copy of function arguments. ordered_replay verifies
canonical projection and native-item digests before interleaving current calls.
A witness detects local changes; it cannot certify decryptability or portability.
Depends only on the stdlib and the stdlib-only state-origin marker owner, so
checked cross-protocol conversion can recognize the internal markers without
importing a provider package.
"""

from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json

from lib.llm.responses_state_origin import STATE_ORIGIN_TYPE

REPLAY_ORDER_TYPE = 'tofu_replay_order'
MAX_REPLAY_ORDER_ENTRIES = 4096  # The existing Responses output-index vocabulary.
MAX_REPLAY_MESSAGE_TEXT_BYTES = 256 * 1024  # Existing phase replay text boundary.


class ReplayOrderError(ValueError):
    """Fixed-code failure; caller/native payload never enters diagnostics."""

    def __init__(self, code='native_replay_projection_mismatch'):
        self.code = code
        super().__init__(code)


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ReplayOrderError() from None
    return hashlib.sha256(encoded).hexdigest()


def _call_projection(call):
    function = call.get('function') or {}
    projected = {'id': call.get('id', ''), 'type': call.get('type', 'function'),
                 'name': function.get('name', ''),
                 'arguments': function.get('arguments', '')}
    if call.get('caller') is not None:
        projected['caller'] = call['caller']
    return projected


def _projection(message):
    # The outbound adapter gives native blocks precedence over projected prose.
    # Binding only `content` would let a later redaction override replay stale
    # Responses text. A different effective carrier invalidates the witness.
    content = message.get('_anthropic_content_blocks') or message.get('content') or ''
    # Tool-round reconstruction strips outer prose whitespace. This known
    # projection is not permission to normalize arguments or opaque values.
    if isinstance(content, str):
        content = content.strip()
    return {'content': content,
            'calls': [_call_projection(call)
                      for call in message.get('tool_calls') or []]}


def _native_call_projection(item, reverse):
    projected = {'id': item.get('call_id', ''), 'type': 'function',
                 'name': reverse.get(item.get('name', ''), item.get('name', '')),
                 'arguments': item.get('arguments') or ''}
    caller = deepcopy(item.get('caller'))
    agent = item.get('agent')
    if isinstance(agent, dict) and agent.get('agent_name'):
        if caller is None:
            caller = {'type': 'multi_agent'}
        if isinstance(caller, dict):
            caller['agent_name'] = agent['agent_name']
    if caller is not None:
        projected['caller'] = caller
    return projected


def _message_item(item):
    if item.get('role', 'assistant') != 'assistant' or item.get('agent') is not None:
        raise ReplayOrderError('native_replay_unrepresentable')
    parts = item.get('content') or []
    if not isinstance(parts, list):
        raise ReplayOrderError('native_replay_unrepresentable')
    saved_parts = []
    for part in parts:
        field = ({'output_text': 'text', 'refusal': 'refusal'}.get(part.get('type'))
                 if isinstance(part, dict) else None)
        if field is None or not isinstance(part.get(field), str):
            raise ReplayOrderError('native_replay_unrepresentable')
        saved_parts.append({'type': part['type'], field: part[field]})
    saved = {'type': 'message', 'role': 'assistant', 'content': saved_parts}
    if 'phase' in item:
        if item['phase'] not in ('commentary', 'final_answer', None):
            raise ReplayOrderError('native_replay_unrepresentable')
        saved['phase'] = item['phase']
    return saved


def capture_replay_order(output, message, native_items, *, tool_name_reverse=None):
    """Retain an order witness only when native state meets prose or calls.

    Optional phase-only replay keeps its existing representation and overflow
    behavior. Unrepresentable mandatory interleaving retains a fixed refusal
    marker, not a truncated apparently-valid native sequence.
    """
    saved = list(native_items or [])
    opaque_indices = [index for index, item in enumerate(saved)
                      if item.get('type') != 'message']
    if not opaque_indices:
        return saved
    # Canonical opaque -> prose -> calls already preserves the native order.
    # Historical sidecars need no new marker or duplicated phase text there.
    kinds = [item.get('type') for item in output or []]
    ranks = [2 if kind == 'function_call' else 1 if kind == 'message' else 0
             for kind in kinds]
    if ranks == sorted(ranks):
        return saved
    marker = {'type': REPLAY_ORDER_TYPE, 'version': 1}
    try:
        if len(output) > MAX_REPLAY_ORDER_ENTRIES:
            raise ReplayOrderError('native_replay_unrepresentable')
        call_positions = defaultdict(deque)
        for index, call in enumerate(message.get('tool_calls') or []):
            call_positions[_digest(_call_projection(call))].append(index)
        opaque_queue = deque(opaque_indices)
        phase_positions = defaultdict(deque)
        for index, item in enumerate(saved):
            if item.get('type') == 'message':
                phase_positions[_digest(item)].append(index)
        sequence = []
        text_parts = []
        text_bytes = 0
        reverse = tool_name_reverse or {}
        for item in output:
            kind = item.get('type')
            if kind == 'function_call':
                positions = call_positions[_digest(_native_call_projection(item, reverse))]
                if not positions:
                    raise ReplayOrderError()
                sequence.append(['call', positions.popleft()])
            elif kind == 'message':
                projected = _message_item(item)
                for part in projected['content']:
                    text = part.get('text', part.get('refusal', ''))
                    text_bytes += len(text.encode('utf-8'))
                    if text_bytes > MAX_REPLAY_MESSAGE_TEXT_BYTES:
                        raise ReplayOrderError('native_replay_unrepresentable')
                    text_parts.append(text)
                positions = phase_positions[_digest(projected)]
                if positions:
                    index = positions.popleft()
                else:
                    index = len(saved)
                    saved.append(projected)
                sequence.append(['item', index])
            elif opaque_queue and saved[opaque_queue[0]] == item:
                sequence.append(['item', opaque_queue.popleft()])
        if opaque_queue or any(call_positions.values()):
            raise ReplayOrderError()
        content = message.get('content') or ''
        if not isinstance(content, str) or not any(
                content.strip() == separator.join(text for text in text_parts if text).strip()
                for separator in ('', '\n')):
            raise ReplayOrderError()
        marker.update(sequence=sequence, sequence_digest=_digest(sequence),
                      projection_digest=_digest(_projection(message)), native_digest=_digest(saved))
    except (ReplayOrderError, AttributeError, TypeError, UnicodeError):
        # Keep the original native values. Nothing from a partial projection
        # may be mistaken for an executable replay plan.
        saved = list(native_items or [])
        marker['error'] = 'native_replay_unrepresentable'
    return [*saved, marker]


def ordered_replay(message, function_calls, *, allowed_types):
    """Return proven order, or None for historical sidecars without a witness.

    Function references select current, already-validated canonical wire calls;
    they can never resurrect captured arguments, approvals, or removed calls.
    """
    stored = message.get('_responses_items') or []
    markers = [item for item in stored
               if isinstance(item, dict) and item.get('type') == REPLAY_ORDER_TYPE]
    if not markers:
        return None
    if len(markers) != 1:
        raise ReplayOrderError()
    marker = markers[0]
    # Dispatch appends the state-origin marker after this witness was
    # captured; it is provenance, not native output, so the digests skip it.
    native = [item for item in stored
              if item.get('type') not in (REPLAY_ORDER_TYPE, STATE_ORIGIN_TYPE)]
    if len(native) > MAX_REPLAY_ORDER_ENTRIES:
        raise ReplayOrderError()
    if marker.get('error'):
        raise ReplayOrderError('native_replay_unrepresentable')
    sequence = marker.get('sequence')
    if (marker.get('version') != 1 or not isinstance(sequence, list)
            or len(sequence) > MAX_REPLAY_ORDER_ENTRIES
            or marker.get('sequence_digest') != _digest(sequence)
            or marker.get('projection_digest') != _digest(_projection(message))
            or marker.get('native_digest') != _digest(native)):
        raise ReplayOrderError()
    seen_items = set()
    seen_calls = set()
    out = []
    for reference in sequence:
        if (not isinstance(reference, list) or len(reference) != 2
                or not isinstance(reference[1], int) or isinstance(reference[1], bool)
                or reference[1] < 0):
            raise ReplayOrderError()
        lane, index = reference
        if lane == 'call':
            if index >= len(function_calls) or index in seen_calls:
                raise ReplayOrderError()
            seen_calls.add(index)
            out.append(function_calls[index])
        elif lane == 'item':
            if index >= len(native) or index in seen_items:
                raise ReplayOrderError()
            seen_items.add(index)
            item = native[index]
            kind = item.get('type')
            if kind != 'message' and kind not in allowed_types:
                raise ReplayOrderError('mandatory_continuity_unrepresentable')
            correlation = item.get('call_id') if 'call_id' in item else item.get('id')
            if kind in ('program', 'program_output') and not str(correlation or '').strip():
                continue  # Existing orphan omission; no parent is fabricated.
            out.append(deepcopy(item))
        else:
            raise ReplayOrderError()
    if (seen_items != set(range(len(native)))
            or seen_calls != set(range(len(function_calls)))):
        raise ReplayOrderError()
    return out
