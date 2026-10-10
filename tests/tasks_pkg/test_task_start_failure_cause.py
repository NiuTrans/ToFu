#!/usr/bin/env python3
"""tests/tasks_pkg/test_task_start_failure_cause.py — executor-startup failures must
tell the user WHY, not just THAT the executor did not start.

A ``task_start_failed`` envelope previously carried a fixed detail sentence
("The agent worker could not begin task execution.") that shadowed the real
cause in ``raw`` (e.g. ``storage.v2 peer closed mid-frame`` — the storage
process died mid-response). The cause translation now lives in
``lib.error_envelope.task_start_failure_cause`` and feeds both production
sites: ``lib.tasks_pkg.spawn._finalize_rejected_submission`` and
``lib.conversation_sync.command_service.attempt_start_error_envelope``.
"""

from __future__ import annotations

import pytest

from lib.error_envelope import task_start_failure_cause
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


# ── cause translation ─────────────────────────────────────────────────

def test_mid_frame_peer_close_is_explained_as_storage_crash():
    cause = task_start_failure_cause(StorageError(
        'database_protocol_error',
        'storage.v2 protocol violation: storage.v2 peer closed mid-frame',
    ))
    assert 'mid-response' in cause['detail']
    assert 'crashed or was restarting' in cause['detail']
    # The original technical text stays visible for diagnosis.
    assert 'peer closed mid-frame' in cause['detail']
    assert cause['hint'] is not None
    assert '存储' in cause['hint']
    assert 'Retry' in cause['hint']
    assert 'logs/error.log' in cause['hint']


def test_transient_storage_unavailability_is_explained():
    cause = task_start_failure_cause(StorageError(
        'database_unavailable', 'storage.v2 authority is unavailable',
        True, 100,
    ))
    assert 'temporarily unavailable' in cause['detail']
    assert 'database_unavailable' in cause['detail']
    assert cause['hint'] is not None and '存储' in cause['hint']


def test_unclassified_storage_failure_keeps_its_code():
    cause = task_start_failure_cause(StorageError(
        'database_integrity', 'checksum mismatch on commit',
    ))
    assert 'database_integrity' in cause['detail']
    assert 'checksum mismatch' in cause['detail']
    assert cause['hint'] is not None


def test_executor_shutdown_is_explained_as_server_restart():
    cause = task_start_failure_cause(
        RuntimeError('cannot schedule new futures after shutdown'))
    assert 'shutting down or restarting' in cause['detail']
    assert cause['hint'] is not None
    assert 'Retry' in cause['hint']


def test_unknown_exception_surfaces_its_own_message_without_hint_override():
    cause = task_start_failure_cause(RuntimeError('boom while binding'))
    assert cause['detail'] == 'boom while binding'
    assert cause['hint'] is None


def test_empty_exception_message_falls_back_to_the_type_name():
    cause = task_start_failure_cause(RuntimeError())
    assert cause['detail'] == 'RuntimeError'
    assert cause['hint'] is None


def test_long_exception_messages_are_bounded():
    cause = task_start_failure_cause(RuntimeError('x' * 1000))
    assert len(cause['detail']) <= 140


# ── spawn settlement envelope ─────────────────────────────────────────

def _capture_finalize(monkeypatch):
    calls = []
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.finalize_chat_task_error',
        lambda task, error, **kwargs: calls.append((task, error, kwargs)),
    )
    return calls


def test_spawn_envelope_carries_the_storage_cause(monkeypatch):
    import lib.tasks_pkg.spawn as spawn

    calls = _capture_finalize(monkeypatch)
    task = {
        'id': 'task-storage', 'convId': 'conv-storage',
        'status': 'pending', 'config': {'model': 'test-model'},
    }

    spawn._finalize_rejected_submission(task, StorageError(
        'database_protocol_error',
        'storage.v2 protocol violation: storage.v2 peer closed mid-frame',
    ))

    assert len(calls) == 1
    envelope = calls[0][1]
    assert envelope['kind'] == 'task_start_failed'
    assert envelope['retryable'] is True
    assert 'could not begin task execution' in envelope['detail']
    assert 'mid-response' in envelope['detail']
    assert 'peer closed mid-frame' in envelope['raw']
    assert '存储' in envelope['hint']
    assert calls[0][2] == {'flow_reason': 'executor_start_failed'}


def test_spawn_envelope_keeps_default_hint_for_unknown_causes(monkeypatch):
    import lib.tasks_pkg.spawn as spawn

    calls = _capture_finalize(monkeypatch)
    task = {
        'id': 'task-generic', 'convId': 'conv-generic',
        'status': 'pending', 'config': {'model': 'test-model'},
    }

    spawn._finalize_rejected_submission(task, RuntimeError('binding exploded'))

    envelope = calls[0][1]
    assert envelope['kind'] == 'task_start_failed'
    assert envelope['detail'].endswith('binding exploded')
    # Unknown causes keep the kind's default bilingual recovery hint.
    assert '已安全终止' in envelope['hint']


# ── conversation-sync attempt-start envelope ──────────────────────────

def test_attempt_start_envelope_without_exception_keeps_legacy_shape():
    from lib.conversation_sync.command_service import (
        attempt_start_error_envelope,
    )

    envelope = attempt_start_error_envelope()
    assert envelope['kind'] == 'task_start_failed'
    assert envelope['detail'] == (
        'The durable attempt was created, but no executor task started.')
    assert envelope['raw'] == ''


def test_attempt_start_envelope_with_exception_carries_cause_and_raw():
    from lib.conversation_sync.command_service import (
        attempt_start_error_envelope,
    )

    envelope = attempt_start_error_envelope(StorageError(
        'database_protocol_error',
        'storage.v2 protocol violation: storage.v2 peer closed mid-frame',
    ))
    assert envelope['kind'] == 'task_start_failed'
    assert 'no executor task started' in envelope['detail']
    assert 'mid-response' in envelope['detail']
    assert envelope['raw'] == (
        'storage.v2 protocol violation: storage.v2 peer closed mid-frame')
    assert '存储' in envelope['hint']
