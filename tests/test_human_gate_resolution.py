"""Server-side single-resolution fences for shared human gates."""

from __future__ import annotations

import threading

import pytest

from lib.tasks_pkg import approval, human_guidance


pytestmark = pytest.mark.unit


def test_write_approval_is_first_resolution_wins():
    approval_id = 'test-approval-single-resolution'
    entry = {
        'event': threading.Event(),
        'approved': False,
        'resolved': False,
    }
    with approval._write_approvals_lock:
        approval._write_approvals[approval_id] = entry
    try:
        assert approval.resolve_write_approval(approval_id, True)
        assert not approval.resolve_write_approval(approval_id, False)
        assert entry['approved'] is True
        assert entry['resolved'] is True
        assert entry['event'].is_set()
    finally:
        with approval._write_approvals_lock:
            approval._write_approvals.pop(approval_id, None)


def test_human_input_cannot_be_overwritten_or_cancelled_after_resolution():
    guidance_id = 'test-guidance-single-resolution'
    entry = {
        'event': threading.Event(),
        'response': None,
        'resolved': False,
    }
    with human_guidance._human_guidance_lock:
        human_guidance._human_guidance_requests[guidance_id] = entry
    try:
        assert human_guidance.resolve_human_guidance(guidance_id, 'first')
        assert not human_guidance.resolve_human_guidance(
            guidance_id, 'second')
        assert not human_guidance.cancel_human_guidance(guidance_id)
        assert entry['response'] == 'first'
        assert entry['resolved'] is True
        assert entry['event'].is_set()
    finally:
        with human_guidance._human_guidance_lock:
            human_guidance._human_guidance_requests.pop(guidance_id, None)


def test_human_input_cancel_is_also_first_resolution_wins():
    guidance_id = 'test-guidance-cancel-resolution'
    entry = {
        'event': threading.Event(),
        'response': 'unresolved',
        'resolved': False,
    }
    with human_guidance._human_guidance_lock:
        human_guidance._human_guidance_requests[guidance_id] = entry
    try:
        assert human_guidance.cancel_human_guidance(guidance_id)
        assert not human_guidance.resolve_human_guidance(
            guidance_id, 'too late')
        assert entry['response'] is None
        assert entry['resolved'] is True
    finally:
        with human_guidance._human_guidance_lock:
            human_guidance._human_guidance_requests.pop(guidance_id, None)


def test_approval_accepted_at_timeout_boundary_is_not_discarded(monkeypatch):
    approval_id = 'test-approval-timeout-boundary'

    class CrossingEvent:
        def __init__(self):
            self.set_called = False

        def set(self):
            self.set_called = True

        def wait(self, timeout):
            assert timeout == 0
            assert approval.resolve_write_approval(approval_id, True)
            # Model Event.wait() timing out immediately before the resolver
            # obtains the registry lock.
            return False

    monkeypatch.setattr(approval.threading, 'Event', CrossingEvent)

    assert approval.request_write_approval(approval_id, timeout=0) is True
    with approval._write_approvals_lock:
        assert approval_id not in approval._write_approvals


def test_human_response_wins_abort_boundary_when_already_locked(monkeypatch):
    guidance_id = 'test-guidance-abort-boundary'

    class CrossingEvent:
        def __init__(self):
            self.set_called = False
            self.waits = 0

        def set(self):
            self.set_called = True

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                assert human_guidance.resolve_human_guidance(
                    guidance_id, 'first')
                return False
            return self.set_called

    monkeypatch.setattr(human_guidance.threading, 'Event', CrossingEvent)
    monkeypatch.setattr(human_guidance, '_ABORT_POLL_INTERVAL', 0)

    response = human_guidance.request_human_guidance(
        guidance_id, task={'id': 'task-1', 'aborted': True})

    assert response == 'first'
    with human_guidance._human_guidance_lock:
        assert guidance_id not in human_guidance._human_guidance_requests
