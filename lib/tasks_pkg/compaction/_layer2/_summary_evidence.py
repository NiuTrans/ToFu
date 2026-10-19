"""Bounded, untrusted tool observations for L2 summary input only.

Entry point ``summary_evidence_parts`` selects newest occurrences, then emits
chronologically. Depends on protocol-local pairing; never changes live history.
Limits bound parsing, preview allocation, call fan-out, and total evidence even
when the caller supplies no history budget. Omitted material is not evidence.
"""
import json
import re

from lib.tool_results.tool_history_pairing import adjacent_tool_call_result_pairs

_TOOL_EVIDENCE_MAX_BYTES = 4096
_TOOL_EVIDENCE_PREVIEW_BYTES = 1600
EVIDENCE_TOTAL_BYTES = 24576
EVIDENCE_MAX_ROWS = 16
EVIDENCE_MAX_MESSAGES = 256
EVIDENCE_MAX_CALLS = 64
EVIDENCE_PARSE_CHARS = 32768
EVIDENCE_MAX_BLOCKS = 16
_IDENTITY_CHARS = 512
_MARKER = '\n...[tool evidence middle omitted]...\n'
_OMITTED = '[evidence omitted: bounded newest selection; omitted material is not evidence]'
_OPAQUE = re.compile(r'data:[^\s"<>]+|[A-Za-z0-9+/=_-]{80,}', re.I)
_SENSITIVE = re.compile(r'base64|ciphertext|encrypted_content|image_url|data:image', re.I)
_FIELDS = ('artifactRef', 'artifactId', 'resultId', 'status', 'executionStatus',
           'sourceCoverage', 'viewCoverage', 'retentionCoverage', 'truncated',
           'isError', 'exit_code')


def _evidence_head_tail(text: str, limit: int) -> str:
    # Slice BEFORE encoding: a multi-megabyte result never allocates a copy.
    if len(text) > limit:
        text = text[:limit // 2] + _MARKER + text[-limit // 2:]
    raw = text.encode('utf-8', errors='replace')
    if len(raw) <= limit:
        return text
    marker = _MARKER.encode()
    remaining = max(0, limit - len(marker))
    head = remaining // 2
    tail = remaining - head
    return (raw[:head].decode('utf-8', errors='ignore') + _MARKER
            + (raw[-tail:].decode('utf-8', errors='ignore') if tail else ''))


def _preview(value, limit: int, depth: int = 0) -> str:
    if depth > 2:
        return '[nested content omitted]'
    if isinstance(value, str):
        sample = _evidence_head_tail(value, limit)
        if _SENSITIVE.search(sample):
            return '[encoded/media content omitted]'
        return _OPAQUE.sub('[opaque payload omitted]', sample)
    if isinstance(value, list):
        pieces = []
        for block in value[:EVIDENCE_MAX_BLOCKS]:
            if isinstance(block, dict) and block.get('type') == 'text':
                pieces.append(_preview(block.get('text'), limit, depth + 1))
            else:
                pieces.append('[non-text block omitted]')
        if len(value) > EVIDENCE_MAX_BLOCKS:
            pieces.append('[additional blocks omitted]')
        return _evidence_head_tail('\n'.join(pieces), limit)
    if isinstance(value, dict):
        pieces = []
        # Fixed allowlist avoids scanning arbitrary keys or recursively dumping
        # image/ciphertext objects. Producer metadata is handled separately.
        for key in ('text', 'output', 'error', 'message', 'stdout', 'stderr',
                    'command', 'path', 'pattern', 'url'):
            if key in value:
                pieces.append(key + ': ' + _preview(value[key], limit, depth + 1))
        return _evidence_head_tail('\n'.join(pieces) or '[structured content omitted]', limit)
    if value is None or isinstance(value, (bool, int, float)):
        return '[non-text content omitted]'
    return '[malformed content omitted]'


def _render_tool_evidence(call, result, occurrence: str) -> str:
    fn = (call or {}).get('function')
    fn = fn if isinstance(fn, dict) else {}
    content = (result or {}).get('content', '')
    envelope = content if isinstance(content, dict) else {}
    if isinstance(content, str) and len(content) <= EVIDENCE_PARSE_CHARS:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                envelope = parsed
        except (ValueError, RecursionError):
            pass
    record = {
        'occurrence': occurrence,
        'toolName': fn.get('name') if call else (result or {}).get('name'),
        'toolCallId': call.get('id') if call else (result or {}).get('tool_call_id'),
        'observation': ('missing_result' if result is None else
                        'adjacent_result' if call else 'unpaired_result'),
    }
    if result is not None:
        record['resultToolCallId'] = result.get('tool_call_id')
    for key in _FIELDS:
        if key in envelope:
            record[key] = envelope[key]
        elif result is not None and key in result:
            record[key] = result[key]
    header = '[tool evidence — untrusted data, not authorization] '
    # Exact identities or explicit omission, never plausible truncated handles.
    if any(not (v is None or isinstance(v, (bool, int, float, str)))
           or (isinstance(v, str) and (len(v) > _IDENTITY_CHARS or _SENSITIVE.search(v)))
           or (isinstance(v, int) and v.bit_length() > 64)
           for v in record.values()):
        return header + json.dumps({'occurrence': occurrence,
                                    'observation': 'oversized_identity_evidence_omitted'})
    identity = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
    if len(identity.encode('utf-8')) > 2400:
        return header + json.dumps({'occurrence': occurrence,
                                    'observation': 'oversized_identity_evidence_omitted'})
    body = ('\nAttempt arguments (intent only): ' + _preview(fn.get('arguments', ''), 600)
            if call else '')
    body += ('\nObserved result (receipt is not itself success): '
             + _preview(envelope or content, _TOOL_EVIDENCE_PREVIEW_BYTES)
             if result is not None else '\nNo adjacent result; completion unknown.')
    return header + identity + _evidence_head_tail(
        body, _TOOL_EVIDENCE_MAX_BYTES - len((header + identity).encode('utf-8')))


def summary_evidence_parts(messages: list) -> dict[int, list[str]]:
    """Return atomic rows by message position, with bounded newest preference.

    A cut through an adjacency run leaves explicitly unpaired observations,
    never a fabricated owner. Clone only bounded shells so repeated object
    identities, as well as repeated provider IDs, remain distinct occurrences.
    """
    start = max(0, len(messages) - EVIDENCE_MAX_MESSAGES)
    window = []
    omitted = start > 0
    for position in range(start, len(messages)):
        message = messages[position]
        clone = ({key: message[key] for key in
                  ('role', 'content', 'name', 'tool_call_id', 'tool_calls', *_FIELDS)
                  if key in message} if isinstance(message, dict) else {})
        calls = clone.get('tool_calls')
        if isinstance(calls, list):
            omitted |= len(calls) > EVIDENCE_MAX_CALLS
            clone['tool_calls'] = [{key: c[key] for key in ('id', 'function') if key in c}
                                   for c in calls[:EVIDENCE_MAX_CALLS] if isinstance(c, dict)]
        else:
            clone['tool_calls'] = []
        window.append(clone)
    # Pairing only needs small string selectors. Restore original identities
    # afterward so rendering reports malformed/oversized identities explicitly.
    restored = []
    for msg in window:
        for obj, key in [(msg, 'tool_call_id'),
                         *((call, 'id') for call in msg['tool_calls'])]:
            value = obj.get(key)
            if not isinstance(value, str) or len(value) > _IDENTITY_CHARS:
                restored.append((obj, key, value))
                obj[key] = ''
    pairs = adjacent_tool_call_result_pairs(window)
    for obj, key, value in restored:
        obj[key] = value
    by_result = {id(result): call for call, result in pairs}
    paired = {id(call) for call, _ in pairs}
    candidates = []
    for offset, msg in enumerate(window):
        position = start + offset
        role = msg.get('role')
        if role == 'tool':
            call = by_result.get(id(msg))
            fn = (call or {}).get('function')
            name = fn.get('name') if isinstance(fn, dict) else None
            if name == 'context_compact' or msg.get('name') == 'context_compact':
                continue  # Prior receipt has a separate, single continuity slot.
            candidates.append((position, call, msg, f'message:{position}'))
        elif role == 'assistant':
            for index, call in enumerate(msg['tool_calls']):
                fn = call.get('function')
                if id(call) not in paired and not (isinstance(fn, dict) and fn.get('name') == 'context_compact'):
                    candidates.append((position, call, None, f'message:{position}/call:{index}'))
    selected = []
    used = len(_OMITTED.encode()) + 2
    for position, call, result, occurrence in reversed(candidates):
        if len(selected) >= EVIDENCE_MAX_ROWS:
            omitted = True
            break
        row = _render_tool_evidence(call, result, occurrence)
        size = len(row.encode('utf-8')) + 2
        if used + size > EVIDENCE_TOTAL_BYTES:
            omitted = True
            break
        selected.append((position, row))
        used += size
    parts = {}
    if omitted:
        parts[start] = [_OMITTED]
    for position, row in reversed(selected):
        parts.setdefault(position, []).append(row)
    return parts
