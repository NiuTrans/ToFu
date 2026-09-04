"""Public concurrency, ownership, and capacity contracts for human gates."""

from __future__ import annotations

import threading
import time

import pytest

from lib.tasks_pkg import approval, human_guidance, stdin_handler
from lib.tasks_pkg.human_gate_registry import (
    GATE_GUIDANCE,
    GATE_STDIN,
    GATE_WRITE_APPROVAL,
    OwnedHumanGateRegistry,
)


pytestmark = pytest.mark.unit
OWNER = 73
OTHER_OWNER = 74


def _wait_until(predicate, *, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('human gate did not become pending')


def test_registry_binds_owner_and_preserves_first_resolution():
    registry = OwnedHumanGateRegistry(capacity=2)
    entry = registry.register(
        GATE_WRITE_APPROVAL, 'approval-one', owner_user_id=OWNER)

    assert entry is not None
    assert not registry.resolve(
        GATE_WRITE_APPROVAL,
        'approval-one',
        owner_user_id=OTHER_OWNER,
        response=True,
    )
    assert registry.resolve(
        GATE_WRITE_APPROVAL,
        'approval-one',
        owner_user_id=OWNER,
        response=True,
    )
    assert not registry.resolve(
        GATE_WRITE_APPROVAL,
        'approval-one',
        owner_user_id=OWNER,
        response=False,
    )
    resolution = registry.take(
        GATE_WRITE_APPROVAL, 'approval-one', entry)
    assert resolution.found is True
    assert resolution.resolved is True
    assert resolution.response is True
    assert len(registry) == 0


def test_registry_capacity_is_shared_across_gate_kinds():
    registry = OwnedHumanGateRegistry(capacity=2)

    assert registry.register(
        GATE_STDIN, 'same-id', owner_user_id=OWNER) is not None
    assert registry.register(
        GATE_GUIDANCE, 'same-id', owner_user_id=OWNER) is not None
    assert registry.register(
        GATE_WRITE_APPROVAL, 'third', owner_user_id=OWNER) is None


def test_registry_timeout_discard_cannot_erase_a_winning_response():
    registry = OwnedHumanGateRegistry(capacity=1)
    entry = registry.register(
        GATE_STDIN, 'boundary', owner_user_id=OWNER)
    assert entry is not None

    assert registry.resolve(
        GATE_STDIN,
        'boundary',
        owner_user_id=OWNER,
        response='first',
    )
    assert not registry.discard_unresolved(GATE_STDIN, 'boundary', entry)
    assert registry.take(GATE_STDIN, 'boundary', entry).response == 'first'


def test_write_approval_wait_is_owner_bound_and_first_resolution_wins():
    approval_id = 'approval-public-boundary'
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        approval.request_write_approval(
            approval_id, timeout=2, owner_user_id=OWNER)))
    waiter.start()
    _wait_until(lambda: approval.is_write_approval_pending(
        approval_id, owner_user_id=OWNER))

    assert not approval.resolve_write_approval(
        approval_id, True, owner_user_id=OTHER_OWNER)
    assert approval.resolve_write_approval(
        approval_id, True, owner_user_id=OWNER)
    assert not approval.resolve_write_approval(
        approval_id, False, owner_user_id=OWNER)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == [True]


def test_human_guidance_cancel_unblocks_only_its_owner():
    guidance_id = 'guidance-public-cancel'
    task = {'id': 'task-guidance', '_userId': OWNER, 'aborted': False}
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        human_guidance.request_human_guidance(guidance_id, task=task)))
    waiter.start()
    _wait_until(lambda: human_guidance.is_human_guidance_pending(
        guidance_id, owner_user_id=OWNER))

    assert not human_guidance.cancel_human_guidance(
        guidance_id, owner_user_id=OTHER_OWNER)
    assert human_guidance.cancel_human_guidance(
        guidance_id, owner_user_id=OWNER)
    assert not human_guidance.resolve_human_guidance(
        guidance_id, 'too late', owner_user_id=OWNER)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == [None]


def test_stdin_response_unblocks_only_its_owner():
    stdin_id = 'stdin-public-response'
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        stdin_handler.request_stdin(stdin_id, owner_user_id=OWNER)))
    waiter.start()
    _wait_until(lambda: stdin_handler.is_stdin_pending(
        stdin_id, owner_user_id=OWNER))

    assert not stdin_handler.resolve_stdin(
        stdin_id, 'foreign', owner_user_id=OTHER_OWNER)
    assert stdin_handler.resolve_stdin(
        stdin_id, 'first', owner_user_id=OWNER)
    assert not stdin_handler.cancel_stdin(
        stdin_id, owner_user_id=OWNER)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == ['first']


@pytest.mark.parametrize(
    ('invoke', 'message'),
    [
        (lambda: approval.request_write_approval(
            'ownerless', timeout=0, owner_user_id=None), 'owner'),
        (lambda: human_guidance.request_human_guidance(
            'ownerless'), 'task dict'),
        (lambda: stdin_handler.request_stdin('ownerless'), 'task dict'),
    ],
)
def test_ownerless_registration_fails_closed(invoke, message):
    with pytest.raises(ValueError, match=message):
        invoke()
