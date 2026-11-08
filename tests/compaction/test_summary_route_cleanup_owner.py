"""Summary route teardown retains owned handles until physical detachment.

The existing native fixture runs the real compiler, private slot mint and
idempotent disposer. Only the provider transport and one failed release are
synthetic; every injected failure is repaired in fixture cleanup.
"""
from copy import deepcopy

import pytest

from lib.llm.llm_errors import AbortedError
from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
from tests.compaction.test_auxiliary_summary_integration import native as native
from tests.compaction.test_summary_credential_resilience import (
    resilient as resilient, _PARENT_PIN, _active, _complete, _error,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('outcome', ['outage', 'success', 'control_exit'])
def test_summary_retains_disposal_owner_after_failure_before_slot_detach(
        resilient, monkeypatch, outcome):
    from lib.llm_dispatch import ephemeral
    from lib.model_routing import dispatch_adapter

    class ControlExit(BaseException):
        pass

    original = deepcopy(resilient.source)
    dispose = dispatch_adapter.dispose_ephemeral_slots
    live_before = set(ephemeral._handles)
    released, attempts, calls = [], [], []

    def fail_before_detach_once(handles):
        (handle,) = handles  # an auxiliary summary group owns one route
        attempts.append(handle)
        assert handle.slot.is_available is False
        if len(attempts) == 1:
            raise RuntimeError('synthetic release failed before detach')
        released.append(handle)
        return dispose(handles)

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append(slot)
        if outcome == 'outage':
            raise _error()
        if outcome == 'control_exit':
            raise ControlExit('synthetic cancellation')
        return _complete(slot)

    monkeypatch.setattr(dispatch_adapter, 'dispose_ephemeral_slots', fail_before_detach_once)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    try:
        expected = ControlExit if outcome == 'control_exit' else RuntimeError
        with provider_pin(_PARENT_PIN), pytest.raises(expected):
            resilient.summary._generate_query_aware_summary(
                resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() is None
        assert resilient.source == original
        assert len(calls) == (3 if outcome == 'outage' else 1)
        assert len(attempts) == 2 and attempts[0] is attempts[1]
        assert released == [attempts[0]] and released[0].disposed
        assert resilient.pool.slots == [resilient.parent]
        assert set(ephemeral._handles) == live_before
        assert all(slot.api_key == '' for slot in calls)
        assert usage['_summaryFailure'] == {'code': 'summary_dispatch_failed'}
        from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure
        assert reconstructed_summary_failure(resilient.task, summary_usage=usage) is None
        assert resilient.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_dispatch_failed'
    finally:
        # A red pre-fix result must not leak the fixture's orphan to later tests.
        for handle in list(ephemeral._handles.values()):
            if handle.handle_id not in live_before:
                dispose([handle])


def test_owner_stop_and_cleanup_fault_keep_primary_cancel_without_route_replay(
        resilient, monkeypatch):
    from lib.llm_dispatch import ephemeral
    from lib.model_routing import dispatch_adapter

    dispose = dispatch_adapter.dispose_ephemeral_slots
    live_before = set(ephemeral._handles)
    attempts = []

    def fail_before_detach_once(handles):
        (handle,) = handles  # an auxiliary summary group owns one route
        attempts.append(handle)
        assert handle.slot.is_available is False
        if len(attempts) == 1:
            raise RuntimeError('synthetic release failed before detach')
        return dispose(handles)

    def stream(*args, **kwargs):
        resilient.task['aborted'] = True
        raise AbortedError('synthetic owner Stop')

    monkeypatch.setattr(dispatch_adapter, 'dispose_ephemeral_slots', fail_before_detach_once)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    try:
        # No summary can be adopted; cleanup's own failure stays visible.
        with provider_pin(_PARENT_PIN), pytest.raises(RuntimeError):
            resilient.summary._generate_query_aware_summary(
                resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert len(attempts) == 2 and attempts[0] is attempts[1]
        assert resilient.pool.slots == [resilient.parent]
        assert set(ephemeral._handles) == live_before
        assert '_summaryFailure' not in usage
        assert resilient.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_cancelled'
    finally:
        for handle in list(ephemeral._handles.values()):
            if handle.handle_id not in live_before:
                dispose([handle])


def test_persistent_disposal_fault_remains_explicit_bounded_and_uncallable(
        resilient, monkeypatch):
    from lib.llm_dispatch import ephemeral
    from lib.model_routing import dispatch_adapter

    dispose = dispatch_adapter.dispose_ephemeral_slots
    live_before = set(ephemeral._handles)
    attempts = []

    def fail_before_detach(handles):
        (handle,) = handles  # an auxiliary summary group owns one route
        attempts.append(handle)
        assert handle.slot.is_available is False
        raise RuntimeError('persistent synthetic release refusal')

    def stream(*args, **kwargs):
        return _complete(_active(resilient))

    monkeypatch.setattr(dispatch_adapter, 'dispose_ephemeral_slots', fail_before_detach)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    try:
        with provider_pin(_PARENT_PIN), pytest.raises(RuntimeError) as raised:
            resilient.summary._generate_query_aware_summary(
                resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert len(attempts) == 2 and attempts[0] is attempts[1]
        assert isinstance(raised.value.__cause__, RuntimeError)
        handle = attempts[0]
        assert not handle.disposed and handle.handle_id in ephemeral._handles
        assert handle.slot.is_available is False
        assert handle.slot.api_key == '' and handle.slot.extra_headers == {}
        assert usage['_summaryFailure'] == {'code': 'summary_dispatch_failed'}
        from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure
        assert reconstructed_summary_failure(resilient.task, summary_usage=usage) is None
        assert resilient.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_dispatch_failed'
    finally:
        for handle in list(ephemeral._handles.values()):
            if handle.handle_id not in live_before:
                dispose([handle])
