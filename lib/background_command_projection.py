"""Pure presentation of durable background-command completion evidence.

Queue list and conversation snapshots share this projection. It never owns
execution or delivery state; a remaining row means delivery is unconfirmed,
not that the subprocess is still running.
"""
from __future__ import annotations

from typing import Any, Mapping


def bounded_completion_preview(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Bound display text without repairing identity, status, or malformed types."""
    from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS

    result = dict(metadata)
    properties = OPENAPI_SCHEMAS['BackgroundCommandCompletion']['properties']
    for field in ('command', 'resultPreview'):
        value = result.get(field)
        if isinstance(value, str):
            limit = properties[field]['maxLength']
            if len(value) > limit:
                result[field] = value[:limit]
                if field == 'resultPreview':
                    result['resultTruncated'] = True
    return result


def bound_projection_completion_previews(projection: dict[str, Any]) -> None:
    """Copy nested display metadata; never mutate durable input or model text."""
    completion = projection.get('backgroundCommand')
    if isinstance(completion, Mapping):
        projection['backgroundCommand'] = bounded_completion_preview(completion)
    injections = projection.get('_bgCommandInjects')
    if not isinstance(injections, list):
        return
    normalized = []
    for injection in injections:
        if not isinstance(injection, Mapping) or not isinstance(injection.get('previews'), list):
            normalized.append(injection)
            continue
        previews = []
        for preview in injection['previews']:
            if isinstance(preview, Mapping) and isinstance(preview.get('backgroundCommand'), Mapping):
                preview = {**preview, 'backgroundCommand': bounded_completion_preview(preview['backgroundCommand'])}
            previews.append(preview)
        normalized.append({**injection, 'previews': previews})
    projection['_bgCommandInjects'] = normalized


def background_command_injection_record(
    event: Mapping[str, Any], *, attempt_id: str,
) -> dict[str, Any]:
    """Build one retry-stable receipt; queue IDs identify delivery, not execution."""
    import hashlib
    import json

    previews = event.get('previews')
    if not isinstance(previews, list) or not previews:
        raise ValueError('Invalid background-command receipt batch')
    if any(not isinstance(preview, dict) for preview in previews):
        raise ValueError('Invalid background-command receipt preview')
    # Include the round and exact occurrence list; a lost-ACK rebase must not
    # append a second block, even when its task-local sidecar was never updated.
    identity = json.dumps([attempt_id, event.get('roundNum'), previews],
                          sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return {
        'blockId': f'injection:background-command:{digest}',
        'round': event['roundNum'], 'count': len(previews),
        'previews': [dict(preview) for preview in previews],
    }


def command_result_status(result: str) -> dict[str, Any]:
    """Extract only the executor's terminal trailer, never arbitrary stdout."""
    import re

    # Native subprocess statuses fit in ten decimal digits. Bound parsing
    # before integer conversion so corrupt receipts cannot abort settlement.
    match = re.search(r'\n\[exit code: (-?[0-9]{1,10})\]\s*$', result)
    if not match:
        return {}
    trailer = result[:match.start()]
    return {
        'exitCode': int(match.group(1)),
        'timedOut': trailer.endswith('\n[Command timed out]'),
        'interrupted': bool(re.search(
            r'\n\[Command (?:interrupted by [^\n]+|aborted by user)\]$', trailer)),
    }


def completion_outcome(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Only producer-stamped exit evidence survives; completion alone is neutral."""
    outcome = {}
    if type(metadata.get('exitCode')) is int:
        outcome['exitCode'] = metadata['exitCode']
    for key in ('timedOut', 'interrupted'):
        if type(metadata.get(key)) is bool:
            outcome[key] = metadata[key]
    return outcome


def background_command_completion(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project explicit producer metadata; never infer success from output text."""
    command_id = payload.get('_backgroundCommand')
    if not isinstance(command_id, str) or not command_id:
        return None
    metadata = payload.get('_backgroundCommandCompletion')
    if not isinstance(metadata, Mapping):
        # Old durable rows remain readable without interpreting model-facing XML.
        metadata = {}
    return bounded_completion_preview({
        'commandId': command_id[:128],
        'status': 'completed',
        'command': str(metadata.get('command') or ''),
        'resultPreview': str(metadata.get('resultPreview') or ''),
        'resultTruncated': bool(metadata.get('resultTruncated')),

        **completion_outcome(metadata),
    })


def normalize_handoff_round(round_record: dict[str, Any]) -> dict[str, Any]:
    """Read-compatibility for pre-ID automatic handoffs; never parse in the UI.

    Only the executor's exact terminal handoff envelope on an explicitly
    backgrounded command is eligible. Existing structured IDs always win.
    """
    import re

    if round_record.get('toolName') not in {'run_command', 'code_exec'}:
        return round_record
    results = round_record.get('results')
    if not isinstance(results, list):
        return round_record
    normalized = []
    for meta in results:
        if (not isinstance(meta, Mapping) or meta.get('backgroundCommandId')
                or not (meta.get('backgrounded') or meta.get('exitCode') == 'background')):
            normalized.append(meta)
            continue
        text = str(round_record.get('content') or meta.get('output') or '')
        match = re.search(
            r'\n\[Command moved to background: (bg_[a-f0-9]{12})\. '
            r'The command is still running;[^\n]*\]\s*$', text)
        command_id = round_record.get('backgroundCommandId') or (match.group(1) if match else None)
        normalized.append({**meta, 'backgroundCommandId': command_id} if command_id else meta)
    return {**round_record, 'results': normalized}
