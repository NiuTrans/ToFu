"""Translate native wire semantics into the backend-neutral application port.

Native diagnostics may expose bounded progress envelopes; application operations
retain their existing result shapes. This is the sole owner of that adaptation,
not repositories, UI consumers or the standalone tofu_db transport package.
"""
from __future__ import annotations

from typing import Any, Mapping
from lib.storage.errors import StorageError
from lib.conversation_sync.settings_policy import public_conversation_settings
from lib.storage.protocol import validate_finite_json_numbers
from tofu_db.generated_v2 import COMMIT_NOTICE_CONTRACT, MAX_COMMIT_NOTICE_KEY_BYTES
from lib.storage.generated_v2_semantics import BOUNDED_PROGRESS_OPERATIONS, OWNER_OMITTED_OPERATIONS, NOTIFICATION_TOPICS


def request_payload(operation: str, payload: Mapping[str, Any] | None, *, owner_id: int) -> dict[str, Any]:
    body = dict(payload or {})
    # Payload-free system queries and lease-fenced worker commands carry no
    # application owner field. The wire session still carries its authority
    # scope; adding an owner would violate these operations' strict contract.
    if operation not in OWNER_OMITTED_OPERATIONS:
        body.setdefault('user_id', owner_id)
    rule = BOUNDED_PROGRESS_OPERATIONS.get(operation)
    if rule is not None:
        limit = body.get(rule['requestLimitField'], rule['defaultLimit'])
        if type(limit) is not int or not 1 <= limit <= rule['maximumLimit']:
            raise StorageError('database_protocol_error', f'Invalid {operation} limit')
        # One native maintenance transaction has its own physical bound.
        # The application asks for *at most* limit, never an exact
        # count. Do not widen engine transaction budgets to match the caller.
        body[rule['requestLimitField']] = min(limit, rule['nativeMaximumLimit'])
    # The same admission the storage.v1 frame encoder enforces: JSON cannot
    # represent NaN/Infinity, and the v2 encoders disagree on them (stdlib
    # emits bare NaN/Infinity tokens, orjson raises TypeError). Reject with
    # the v1 error class before the divergence can escape as a native
    # exception instead of a typed storage error.
    validate_finite_json_numbers(body)
    return body


def application_result(operation: str, result: Any) -> Any:
    if operation == 'turn.sync.snapshot' and isinstance(result, Mapping):
        settings = result.get('settings')
        if isinstance(settings, Mapping):
            result = {**result, 'settings': public_conversation_settings(settings)}
    rule = BOUNDED_PROGRESS_OPERATIONS.get(operation)
    if rule is None:
        return result
    count = result.get(rule['resultCountField']) if isinstance(result, Mapping) else None
    if type(count) is not int or not 0 <= count <= rule['nativeMaximumLimit']:
        raise StorageError('database_protocol_error', f'Malformed {operation} progress response')
    return count


def application_notice(notice: Mapping[str, Any], *, owner_id: int | None = None) -> Mapping[str, Any]:
    """Map engine topic/key hints into the application's observer contract."""
    if notice.get('contract') != COMMIT_NOTICE_CONTRACT:
        return notice
    topic = notice.get('topic')
    if not isinstance(topic, str):
        raise StorageError('database_protocol_error', 'Malformed committed storage notification topic')
    rule = NOTIFICATION_TOPICS.get(topic)
    if rule is None:
        return notice
    owner, key, sequence = notice.get('userId'), notice.get('key'), notice.get('sequence')
    try:
        key_bytes = len(key.encode('utf-8')) if isinstance(key, str) else 0
    except UnicodeEncodeError:
        key_bytes = 0
    if (type(owner) is not int or owner <= 0 or (owner_id is not None and owner != owner_id)
            or not 1 <= key_bytes <= MAX_COMMIT_NOTICE_KEY_BYTES
            or type(sequence) is not int or sequence <= 0):
        raise StorageError('database_protocol_error', 'Malformed committed storage notification')
    return {'contract': rule['contract'], 'userId': owner,
            'event': {rule['keyField']: key, rule['sequenceField']: sequence}}
