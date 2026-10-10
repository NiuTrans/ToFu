"""``describe_storage_failure`` renders the classified StorageError surface.

Ops logs that interpolate ``str(exc)`` lose the code/retryable/retry_after_ms
fields (the message alone is often a bare, unactionable code); the helper must
surface them, through wrappers, without hiding non-storage failure types.
"""

import pytest

from lib.storage.errors import StorageError, describe_storage_failure

pytestmark = pytest.mark.unit


def test_storage_error_full_surface_rendered():
    exc = StorageError('database_timeout', 'deadline_elapsed', True, 100)
    detail = describe_storage_failure(exc)
    assert 'code=database_timeout' in detail
    assert "message='deadline_elapsed'" in detail
    assert 'retryable=True' in detail
    assert 'retry_after_ms=100' in detail


def test_empty_message_still_shows_the_code():
    exc = StorageError('database_protocol_error', '', False)
    detail = describe_storage_failure(exc)
    assert 'code=database_protocol_error' in detail
    assert "message=''" in detail
    assert 'retryable=False' in detail


def test_wrapped_storage_error_found_through_cause_chain():
    cause = StorageError('database_busy', 'locked', True, 50)
    try:
        try:
            raise cause
        except StorageError as inner:
            raise RuntimeError('event 593 was not durable') from inner
    except RuntimeError as wrapped:
        detail = describe_storage_failure(wrapped)
    assert 'code=database_busy' in detail
    assert 'retry_after_ms=50' in detail


def test_implicit_context_chain_is_walked():
    cause = StorageError('database_unavailable', 'down', True, 100)
    try:
        try:
            raise cause
        except StorageError:
            raise ValueError('outer failure')
    except ValueError as wrapped:
        detail = describe_storage_failure(wrapped)
    assert 'code=database_unavailable' in detail


def test_operation_id_rendered_when_stamped():
    exc = StorageError('database_protocol_error', 'database_protocol_error',
                       False, operation_id='turn.event.record')
    detail = describe_storage_failure(exc)
    assert 'code=database_protocol_error' in detail
    assert 'operation_id=turn.event.record' in detail


def test_non_storage_exception_falls_back_to_type_and_message():
    assert (describe_storage_failure(ValueError('bad input'))
            == 'ValueError: bad input')
