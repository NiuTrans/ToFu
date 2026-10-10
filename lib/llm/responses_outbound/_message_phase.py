"""Bounded assistant phase replay for stateless Responses requests.

Entry points capture_phase_messages/replay_phase_messages retain provider phase
and message boundaries through the existing private Responses history sidecar.
Only unchanged canonical prose may replay captured text; hooks, redaction and
compaction remain authoritative. This leaf depends only on the standard library.
"""

from __future__ import annotations

import copy

MAX_PHASE_MESSAGES = 64
MAX_PHASE_TEXT_BYTES = 256 * 1024
_PHASES = frozenset({'commentary', 'final_answer'})


def capture_phase_messages(items) -> list[dict]:
    """Capture a complete bounded single-agent message sequence, or none.

    Unphased siblings must remain in the sequence too: replay must neither
    duplicate them nor infer that their text belongs to another item's phase.
    Native multi-agent attribution has its own visibility/replay owner.
    """
    messages = []
    text_bytes = 0
    has_phase = False
    for item in items or ():
        if not isinstance(item, dict) or item.get('type') != 'message':
            continue
        if item.get('agent') is not None or item.get('role') != 'assistant':
            return []
        phase = item.get('phase')
        if phase is not None and (not isinstance(phase, str) or phase not in _PHASES):
            return []
        parts = item.get('content')
        if not isinstance(parts, list) or len(parts) > MAX_PHASE_MESSAGES:
            return []
        saved_parts = []
        for part in parts:
            if not isinstance(part, dict):
                return []
            kind = part.get('type')
            field = ({'output_text': 'text', 'refusal': 'refusal'}.get(kind)
                     if isinstance(kind, str) else None)
            if field is None or not isinstance(part.get(field), str):
                return []
            text = part[field]
            if len(text) > MAX_PHASE_TEXT_BYTES:
                return []
            text_bytes += len(text.encode('utf-8'))
            if text_bytes > MAX_PHASE_TEXT_BYTES:
                return []
            saved_parts.append({'type': kind, field: text})
        saved = {'type': 'message', 'role': 'assistant', 'content': saved_parts}
        if 'phase' in item:
            saved['phase'] = phase
        has_phase |= phase in _PHASES
        messages.append(saved)
        if len(messages) > MAX_PHASE_MESSAGES:
            return []
    return messages if has_phase else []


def replay_phase_messages(message: dict) -> list[dict]:
    """Return detached phase items only while their text matches live prose."""
    if message.get('role') != 'assistant':
        return []
    items = capture_phase_messages(message.get('_responses_items'))
    content = message.get('content')
    if not items or not isinstance(content, str):
        return []
    parts = [part.get('text', part.get('refusal', ''))
             for item in items for part in item['content']]
    # Streaming concatenates deltas; non-streaming joins nonempty parts by
    # newline. Tool-round reconstruction strips only outer whitespace.
    if not any(content.strip() == separator.join(p for p in parts if p).strip()
               for separator in ('', '\n')):
        return []
    return copy.deepcopy(items)


def is_commentary_only(message: dict) -> bool:
    items = replay_phase_messages(message)
    return bool(items) and all(item.get('phase') == 'commentary' for item in items)
