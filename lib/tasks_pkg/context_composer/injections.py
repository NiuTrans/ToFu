"""Unified runtime injection writer and bounded Request Inspector receipts.

Snapshot blocks use the existing renderer; chronological events use
``append_injection``. Both share registry policy and ``record_manifest_rows``.
No function fetches data, grants tools, acknowledges delivery, or edits history.
Registered bootstrap contracts retain system authority before dialogue only;
chronological runtime events cannot elevate it. Assistant prefill, tool receipts
and compaction remain protocol-owned exceptions.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any

from .registry import block_spec, injection_spec

MANIFEST_LIMIT = 128
MANIFEST_BYTES_LIMIT = 128 * 1024

# Only these owners have a dedicated UI receipt for the shared inbox carrier.
# New sources default to a system_note; suppressing it is an explicit contract.
_INBOX_DISPLAY_OWNERS = frozenset({'inbox-lanes', 'peer-lane'})
_ALLOWED_FLAGS = frozenset({
    '_isUrlPrefetch', '_isLifecycleCaution', '_isBranchContext',
    '_isPaperToolGuidance',
    '_isVuDirective', '_vuPromptVersion',
})


def record_manifest_rows(task: dict | None, rows: list[dict], *, replace: bool = False) -> None:
    """Keep bounded content-free decisions, including suppression, in one lane."""
    if task is None:
        return
    existing = [] if replace else list(task.get('_contextManifest') or [])
    for row in rows:
        entry = dict(row)
        spec = block_spec(str(entry.get('registryId') or entry.get('id') or ''))
        if spec is not None:
            entry.update(registryId=spec.id, necessity=spec.necessity,
                         trigger=spec.trigger, required=spec.required,
                         maxTokens=spec.max_tokens, lane=spec.lane)
        # Stable repeated block decisions do not accumulate every round. A
        # reused snapshot leaves its original append receipt byte-identical.
        if (entry.get('reused') and entry.get('reason') == 'already_present'
                and any(old.get('id') == entry.get('id') and old.get('hash') == entry.get('hash')
                        for old in existing)
                and not any(old.get('id') == entry.get('id') and not old.get('appended')
                            for old in existing)):
            continue
        if entry.get('eventId') and any(old.get('eventId') == entry['eventId'] for old in existing):
            continue
        if not entry.get('appended'):
            existing = [old for old in existing if not (
                old.get('id') == entry.get('id') and not old.get('appended'))]
        if existing and existing[-1] == entry:
            continue
        existing.append(entry)
    dropped = max(0, len(existing) - MANIFEST_LIMIT)
    existing = existing[-MANIFEST_LIMIT:]
    sizes = [len(json.dumps(row, ensure_ascii=True, default=str).encode('utf-8'))
             for row in existing]
    total = sum(sizes)
    while sizes and total > MANIFEST_BYTES_LIMIT:
        total -= sizes.pop(0)
        existing.pop(0)
        dropped += 1
    if dropped:
        task['_contextManifestDropped'] = int(task.get('_contextManifestDropped') or 0) + dropped
    task['_contextManifest'] = existing


def can_append_injection(messages: list[dict]) -> bool:
    """Do not insert guidance into an outstanding tool-call/result batch."""
    pending: set[str] = set()
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = message.get('role')
        if role == 'tool':
            pending.add(str(message.get('tool_call_id') or ''))
        elif role == 'assistant':
            calls = message.get('tool_calls') or []
            return not calls or all(str(call.get('id') or '') in pending for call in calls)
        elif role in ('user', 'system'):
            return True
    return True


def append_injection(messages: list[dict], injection_id: str, text: str, *,
                     task: dict | None = None, round_num: int | None = None,
                     reason: str = '', contains_human_steer: bool = False,
                     flags: dict | None = None, action: str = 'prompt',
                     record_note: bool = True, display_owner: str = '',
                     media: list[dict] | None = None) -> dict | None:
    """Append one registered chronological carrier and record its exact cost.

    Event identity is chronology, not text equality: identical retries in
    distinct rounds are real events. Domain producers own retry/delivery caps.
    ``injected`` means appended, NEVER provider consumption. Queue ACK remains
    deferred until the existing verified provider-return boundary.

    Runtime events default to a system_note even when their spec omits a kind.
    Dedicated inbox lanes must declare their display owner instead; bootstrap
    contracts belong only to the Inspector, never the conversation timeline.

    ``media`` content blocks (a human steer's images) precede ``text`` in the
    same carrier, the layout of a user message with images; the receipt
    counts their images beside the text cost.
    """
    spec = injection_spec(injection_id)
    if spec.lane not in ('event', 'inbox', 'bootstrap'):
        raise ValueError(f'{injection_id} cannot append a runtime message')
    if flags and set(flags) - _ALLOWED_FLAGS:
        raise ValueError('Unsupported injection classification flag')

    if not record_note and not display_owner:
        raise ValueError('record_note=False requires an explicit display_owner')
    if display_owner and not (
        (injection_id == 'inbox_delivery' and display_owner in _INBOX_DISPLAY_OWNERS)
        or (spec.lane == 'bootstrap' and display_owner == 'inspector')
    ):
        raise ValueError('Unsupported display_owner for this injection')
    if spec.lane == 'bootstrap':
        display = dict(displaySurface='inspector', displayOwner='inspector',
                       displayReason='bootstrap_policy')
    elif display_owner:
        display = dict(displaySurface='sidecar', displayOwner=display_owner,
                       displayReason='dedicated_sidecar')
    elif task is None:
        display = dict(displaySurface='inspector', displayOwner='inspector',
                       displayReason='no_task_projection')
    else:
        display = dict(displaySurface='system_note', displayOwner='context-composer',
                       displayReason='registered_note_kind' if spec.note_kind else 'fallback_note_kind')
    from . import disabled_context_blocks
    if not spec.required and injection_id in disabled_context_blocks((task or {}).get('config')):
        record_manifest_rows(task, [{
            'id': injection_id, 'source': spec.source, 'authority': spec.authority,
            'injected': False, 'appended': False, 'placement': 'tail',
            'reason': 'disabled', 'chars': 0, 'tokens': 0, 'appendedTokens': 0,
            'displaySurface': 'none', 'displayOwner': display['displayOwner'],
            'displayReason': 'disabled',
        }])
        return None
    if not isinstance(text, str) or not text.strip():
        record_manifest_rows(task, [{
            'id': injection_id, 'injected': False, 'appended': False,
            'placement': 'tail', 'reason': reason or 'empty', 'chars': 0, 'tokens': 0,
            'displaySurface': 'none', 'displayOwner': display['displayOwner'],
            'displayReason': 'empty',
        }])
        return None
    if not can_append_injection(messages):
        raise ValueError('Context injection would split an outstanding tool batch')
    if spec.lane == 'bootstrap' and any(m.get('role') != 'system' for m in messages):
        raise ValueError('Bootstrap policy must precede dialogue; runtime cannot elevate authority')
    message: dict[str, Any] = {
        'role': 'system' if spec.lane == 'bootstrap' else 'user',
        'content': [*media, {'type': 'text', 'text': text}] if media else text,
        '_contextInjectionId': injection_id,
    }
    if spec.lane == 'inbox':
        message.update(_isInboxInject=True, _containsHumanSteer=bool(contains_human_steer))
    else:
        message['_isMeta'] = True
    message.update(flags or {})
    from ._render import _count_tokens
    row = {
        'id': injection_id, 'source': spec.source, 'authority': spec.authority,
        'placement': 'system' if spec.lane == 'bootstrap' else 'tail',
        'stability': spec.stability, 'lifecycle': spec.lifecycle,
        'injected': True, 'appended': True, 'delivery': 'appended_not_acknowledged',
        'chars': len(text), 'tokens': _count_tokens(text, str((task or {}).get('model') or '')),
        'hash': hashlib.sha256(text.encode('utf-8')).hexdigest()[:16],
        'reason': (reason or spec.trigger)[:240], 'round': round_num,
        **display,
    }
    if media:
        row['images'] = sum(1 for block in media if block.get('type') == 'image_url')
    # Position/round/text can repeat after compaction or checkpoint recovery.
    # A receipt identity is minted once and travels with that carrier.
    row['eventId'] = uuid.uuid4().hex
    row['appendedTokens'] = row['tokens']
    from lib.tasks_pkg.segments import INJECTED_NOTES_FIELD, record_injected_note

    # Both recorders are synchronous: notes append; manifest replaces its list.
    # Retain only touched preimages, never copy the task or its message history.
    missing = object()
    previous = {key: task.get(key, missing) for key in (
        INJECTED_NOTES_FIELD, '_contextManifest', '_contextManifestDropped',
    )} if task is not None else {}
    notes_before = previous.get(INJECTED_NOTES_FIELD)
    note_count = len(notes_before) if isinstance(notes_before, list) else 0
    messages.append(message)
    try:
        if display['displaySurface'] == 'system_note':
            note = record_injected_note(
                task, llm_round=round_num, kind=spec.note_kind or 'context-injection',
                text=text, action=action, reason=reason or injection_id)
            if note is None:
                raise ValueError(f'{injection_id} could not record its system_note')
            row['blockId'] = note['blockId']
        message['_contextInjection'] = dict(row)
        record_manifest_rows(task, [row])
    except BaseException:
        # A failed projection/receipt write is not an appended input. Producers
        # may retry their drained item only after every touched lane rolls back.
        if messages and messages[-1] is message:
            messages.pop()
        if task is not None:
            if isinstance(notes_before, list):
                del notes_before[note_count:]
            for key, value in previous.items():
                if value is missing:
                    task.pop(key, None)
                else:
                    task[key] = value
        raise
    return message


def record_injection_diagnostic(task: dict | None, injection_id: str, *,
                                reason: str, metrics: dict) -> None:
    """Telemetry-only decisions are visible to debugging, never to the model."""
    spec = injection_spec(injection_id)
    if spec.lane != 'telemetry':
        raise ValueError('Diagnostic requires a telemetry-only registry entry')
    # No free-form payload/credential text enters the metric receipt.
    safe = {key: value for key, value in list(metrics.items())[:24]
            if isinstance(key, str) and len(key) <= 64
            and isinstance(value, (int, float, bool))
            and (not isinstance(value, float) or math.isfinite(value))}
    record_manifest_rows(task, [{
        'id': injection_id, 'source': spec.source, 'placement': 'telemetry',
        'injected': False, 'appended': False, 'chars': 0, 'tokens': 0,
        'reason': reason, 'metrics': safe,
    }])
