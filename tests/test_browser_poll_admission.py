"""Availability and normal-user contracts for the browser poll boundary."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest


pytest_plugins = ('tests._credential_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]


@pytest.fixture(autouse=True)
def _reset_poll_state():
    from lib.browser.poll_admission import (
        reset_browser_poll_admission_for_tests,
    )
    from lib.browser.queue import _state

    reset_browser_poll_admission_for_tests()
    with _state._commands_lock:
        _state._commands.clear()
    with _state._clients_lock:
        _state._clients.clear()
    with _state._async_waiters_lock:
        _state._async_waiters.clear()
    yield
    reset_browser_poll_admission_for_tests()
    with _state._commands_lock:
        _state._commands.clear()
    with _state._clients_lock:
        _state._clients.clear()
    with _state._async_waiters_lock:
        _state._async_waiters.clear()


def _controller(**overrides):
    from lib.browser.poll_admission import BrowserPollAdmission

    defaults = {
        'max_inflight': 8,
        'max_bucket_entries': 16,
        'credential_rpm': 120,
        'owner_rpm': 300,
        'global_rpm': 600,
    }
    defaults.update(overrides)
    return BrowserPollAdmission(**defaults)


def _token(owner_user_id=41, name='browser-poll-admission'):
    from lib.api_keys import create_key

    _row, token = create_key(
        owner_user_id=owner_user_id,
        name=name,
        scopes=['agents:bridge'],
    )
    return token


def _poll(
    client,
    token,
    client_id='browser-a',
    *,
    reported_protocol_version=None,
    **body_overrides,
):
    body = {
        'clientId': client_id,
        'protocolVersion': 2,
        'capabilities': [],
        'results': [],
    }
    body.update(body_overrides)
    headers = {'X-Bridge-Secret': token}
    if reported_protocol_version is not None:
        headers['X-Browser-Protocol-Version'] = str(
            reported_protocol_version)
    return client.post(
        '/api/browser/poll',
        json=body,
        headers=headers,
        scope_base={'client': ('203.0.113.7', 5050)},
    )


def test_pre_auth_concurrency_is_bounded_and_release_restores_capacity():
    controller = _controller(max_inflight=2)
    first, lease_a = controller.enter(credential='token-a', peer='proxy')
    second, lease_b = controller.enter(credential='token-b', peer='proxy')
    denied, lease_c = controller.enter(credential='token-c', peer='proxy')

    assert first.allowed and second.allowed
    assert denied.code == 'browser_poll_capacity'
    assert lease_c is None

    controller.release(lease_a)
    retried, lease_c = controller.enter(credential='token-c', peer='proxy')
    assert retried.allowed and lease_c is not None
    controller.release(lease_b)
    controller.release(lease_c)
    assert controller.snapshot()['active'] == 0


def test_one_device_cannot_consume_another_devices_reserved_capacity():
    controller = _controller(max_inflight=8)
    first, lease_a = controller.enter(
        credential='runaway-device', peer='proxy')
    overlap, lease_b = controller.enter(
        credential='runaway-device', peer='proxy')
    denied, denied_lease = controller.enter(
        credential='runaway-device', peer='proxy')
    normal, normal_lease = controller.enter(
        credential='normal-device', peer='proxy')

    assert first.allowed and overlap.allowed
    assert denied.code == 'browser_poll_credential_capacity'
    assert denied_lease is None
    assert normal.allowed

    controller.release(lease_a)
    controller.release(lease_b)
    controller.release(normal_lease)


def test_rate_state_and_random_credential_churn_stay_bounded():
    controller = _controller(max_bucket_entries=16)
    for index in range(80):
        decision, lease = controller.enter(
            credential=f'random-token-{index}', peer='proxy')
        assert decision.allowed
        controller.release(lease)
    snapshot = controller.snapshot()
    assert snapshot['credentialBuckets'] == 16
    assert 'random-token' not in repr(snapshot)


def test_one_credential_gets_a_generous_but_finite_rate_budget():
    controller = _controller(credential_rpm=30)
    for _ in range(30):
        decision, lease = controller.enter(
            credential='one-device', peer='proxy')
        assert decision.allowed
        controller.release(lease)
    denied, lease = controller.enter(credential='one-device', peer='proxy')
    assert denied.code == 'browser_poll_credential_rate_limited'
    assert denied.retry_after_seconds >= 1
    assert lease is None


def test_protocol_cooldown_skips_work_then_expires_without_manual_repair():
    now = [100.0]
    controller = _controller(
        protocol_cooldown_seconds=60,
        clock=lambda: now[0],
    )
    allowed, lease = controller.enter(credential='old-device', peer='proxy')
    assert allowed.allowed
    controller.release(lease)
    controller.note_protocol_rejection(
        credential='old-device', client_protocol_version=1)

    parked, parked_lease = controller.enter(
        credential='old-device', peer='proxy')
    assert parked.code == 'browser_protocol_upgrade_required'
    assert parked.client_protocol_version == 1
    assert parked_lease is None

    now[0] += 61
    healed, healed_lease = controller.enter(
        credential='old-device', peer='proxy')
    assert healed.allowed
    controller.release(healed_lease)


def test_protocol_cooldown_does_not_spend_normal_clients_shared_budget():
    controller = _controller(
        credential_rpm=30,
        owner_rpm=30,
        global_rpm=30,
    )
    controller.note_protocol_rejection(
        credential='old-device', client_protocol_version=1)

    for _ in range(30):
        parked, parked_lease = controller.enter(
            credential='old-device', peer='proxy')
        assert parked.code == 'browser_protocol_upgrade_required'
        assert parked_lease is None

    normal, normal_lease = controller.enter(
        credential='normal-device', peer='proxy')
    assert normal.allowed
    controller.release(normal_lease)


def test_credential_rate_rejection_cannot_spend_last_shared_token():
    controller = _controller(
        credential_rpm=30,
        owner_rpm=30,
        global_rpm=31,
    )
    for _ in range(30):
        allowed, lease = controller.enter(
            credential='runaway-device', peer='proxy')
        assert allowed.allowed
        controller.release(lease)

    rejected, rejected_lease = controller.enter(
        credential='runaway-device', peer='proxy')
    assert rejected.code == 'browser_poll_credential_rate_limited'
    assert rejected_lease is None

    normal, normal_lease = controller.enter(
        credential='normal-device', peer='proxy')
    assert normal.allowed
    controller.release(normal_lease)


def test_only_current_protocol_hint_clears_same_credential_upgrade_cooldown():
    controller = _controller(protocol_cooldown_seconds=60)
    controller.note_protocol_rejection(
        credential='upgraded-device', client_protocol_version=1)

    future, future_lease = controller.enter(
        credential='upgraded-device',
        peer='proxy',
        reported_protocol_version=3,
    )
    assert future.code == 'browser_protocol_upgrade_required'
    assert future_lease is None

    current, current_lease = controller.enter(
        credential='upgraded-device',
        peer='proxy',
        reported_protocol_version=2,
    )
    assert current.allowed
    controller.release(current_lease)


def test_two_normal_computers_poll_without_rate_or_capacity_errors(
        flask_client, monkeypatch):
    from lib.browser.poll_admission import (
        reset_browser_poll_admission_for_tests,
    )

    async def no_commands(**_kwargs):
        return []

    monkeypatch.setattr(
        'lib.browser.queue.wait_for_commands_async', no_commands)
    reset_browser_poll_admission_for_tests(_controller(max_inflight=4))

    first = _poll(
        flask_client, _token(name='normal-computer-a'), 'normal-computer-a')
    second = _poll(
        flask_client, _token(name='normal-computer-b'), 'normal-computer-b')

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()['commands'] == []
    assert second.get_json()['commands'] == []


def test_old_protocol_is_parked_before_a_second_storage_authentication(
        flask_client, monkeypatch):
    import lib.bridge_auth as bridge_auth
    from lib.browser.poll_admission import (
        reset_browser_poll_admission_for_tests,
    )

    controller = _controller(protocol_cooldown_seconds=60)
    reset_browser_poll_admission_for_tests(controller)
    token = _token(name='old-protocol-cooldown')
    real_resolve = bridge_auth.resolve_bridge_credential
    calls = []

    def counted_resolve(*args, **kwargs):
        calls.append(1)
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(bridge_auth, 'resolve_bridge_credential', counted_resolve)
    first = _poll(
        flask_client, token, 'old-protocol', protocolVersion=1)
    second = _poll(
        flask_client, token, 'old-protocol', protocolVersion=1)

    assert first.status_code == 426
    assert second.status_code == 426
    assert second.headers['Retry-After']
    assert len(calls) == 1, 'cooldown must reject before another storage lookup'


def test_old_protocol_without_capabilities_keeps_actionable_upgrade_response(
        flask_client):
    token = _token(name='old-protocol-without-capabilities')
    response = _poll(
        flask_client,
        token,
        'old-without-capabilities',
        protocolVersion=1,
        capabilities=None,
    )

    assert response.status_code == 426
    assert response.get_json()['code'] == 'browser_protocol_upgrade_required'


def test_upgraded_extension_reuses_token_without_inheriting_old_cooldown(
        flask_client, monkeypatch):
    from lib.browser.poll_admission import (
        reset_browser_poll_admission_for_tests,
    )

    async def no_commands(**_kwargs):
        return []

    monkeypatch.setattr(
        'lib.browser.queue.wait_for_commands_async', no_commands)
    reset_browser_poll_admission_for_tests(
        _controller(protocol_cooldown_seconds=60))
    token = _token(name='same-token-after-upgrade')

    old = _poll(
        flask_client, token, 'same-device', protocolVersion=1)
    upgraded = _poll(
        flask_client,
        token,
        'same-device',
        protocolVersion=2,
        reported_protocol_version=2,
    )

    assert old.status_code == 426
    assert upgraded.status_code == 200


def test_excessive_single_device_polling_returns_actionable_429(
        flask_client, monkeypatch):
    from lib.browser.poll_admission import (
        reset_browser_poll_admission_for_tests,
    )

    async def no_commands(**_kwargs):
        return []

    monkeypatch.setattr(
        'lib.browser.queue.wait_for_commands_async', no_commands)
    reset_browser_poll_admission_for_tests(
        _controller(credential_rpm=30, owner_rpm=300, global_rpm=600))
    token = _token(name='runaway-device')
    for _ in range(30):
        assert _poll(flask_client, token, 'runaway-device').status_code == 200
    rejected = _poll(flask_client, token, 'runaway-device')
    body = rejected.get_json()

    assert rejected.status_code == 429
    assert int(rejected.headers['Retry-After']) >= 1
    assert body['code'] == 'browser_poll_credential_rate_limited'


def test_results_batch_has_a_server_enforced_protocol_bound(flask_client):
    token = _token(name='oversize-result-batch')
    response = _poll(
        flask_client,
        token,
        'oversize-results',
        results=[{'id': f'cmd-{index}', 'result': None}
                 for index in range(65)],
    )
    assert response.status_code == 413
    assert response.get_json()['code'] == 'browser_poll_results_too_large'


@pytest.mark.parametrize('body_overrides', [
    {'clientId': {'nested': 'device'}},
    {'capabilities': [{'nested': 'tabs'}]},
    {'capabilities': ['tabs'] * 64},
    {'results': [{'id': {'nested': 'command'}, 'result': None}]},
    {'results': [{'id': 'x' * 129, 'result': None}]},
    {'extVersion': {'nested': '5.4.1'}},
    {'profile': 'x' * 81},
])
def test_poll_rejects_structural_amplification_before_registry_or_settlement(
        flask_client, body_overrides):
    token = _token(name='structural-amplification')
    response = _poll(
        flask_client,
        token,
        'bounded-device',
        **body_overrides,
    )

    assert response.status_code == 400


def test_client_registry_refuses_live_churn_but_reuses_disconnected_slots(
        monkeypatch):
    import lib.browser.queue._registry as registry
    from lib.browser.queue import BrowserPollCapacityExceeded, mark_poll
    from lib.browser.queue import _state

    monkeypatch.setattr(registry, '_CLIENT_REGISTRY_MAX', 2)
    monkeypatch.setattr(registry, '_CLIENT_REGISTRY_PER_OWNER', 2)
    for client_id in ('device-a', 'device-b'):
        mark_poll(
            client_id,
            owner_user_id='41',
            protocol_version=2,
            capabilities=[],
        )
    with pytest.raises(BrowserPollCapacityExceeded):
        mark_poll(
            'attacker-churn',
            owner_user_id='41',
            protocol_version=2,
            capabilities=[],
        )

    with _state._clients_lock:
        _state._clients['device-a']['last_poll'] = time.time() - 16
    mark_poll(
        'replacement-device',
        owner_user_id='41',
        protocol_version=2,
        capabilities=[],
    )
    with _state._clients_lock:
        assert set(_state._clients) == {'device-b', 'replacement-device'}


def test_waiter_capacity_rejects_unique_churn_without_dropping_live_waiter(
        monkeypatch):
    import lib.browser.queue._dispatch as dispatch
    from lib.browser.queue import BrowserPollCapacityExceeded
    from lib.browser.queue import _state

    monkeypatch.setattr(dispatch, '_POLL_WAITER_MAX', 1)
    monkeypatch.setattr(dispatch, '_POLL_WAITER_PER_OWNER', 1)

    async def exercise():
        first = asyncio.create_task(dispatch.wait_for_commands_async(
            timeout=5, client_id='device-a', owner_user_id='41'))
        await asyncio.sleep(0.05)
        with pytest.raises(BrowserPollCapacityExceeded):
            await dispatch.wait_for_commands_async(
                timeout=0.1, client_id='device-b', owner_user_id='41')
        with _state._async_waiters_lock:
            assert len(_state._async_waiters) == 1
            assert _state._async_waiters[0]['client_id'] == 'device-a'
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    asyncio.run(exercise())


def test_duplicate_device_poll_is_seamlessly_replaced():
    import lib.browser.queue._dispatch as dispatch
    from lib.browser.queue import _state

    async def exercise():
        first = asyncio.create_task(dispatch.wait_for_commands_async(
            timeout=5, client_id='same-device', owner_user_id='41'))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(dispatch.wait_for_commands_async(
            timeout=5, client_id='same-device', owner_user_id='41'))
        assert await asyncio.wait_for(first, timeout=1) == []
        with _state._async_waiters_lock:
            assert len(_state._async_waiters) == 1
            assert _state._async_waiters[0]['client_id'] == 'same-device'
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

    asyncio.run(exercise())


def test_command_delivery_is_batched_without_losing_remaining_commands():
    from lib.browser.queue import MAX_COMMANDS_PER_POLL, get_pending_commands
    from lib.browser.queue import _state

    with _state._commands_lock:
        for index in range(MAX_COMMANDS_PER_POLL + 8):
            command_id = f'command-{index}'
            _state._commands[command_id] = {
                'id': command_id,
                'type': 'list_tabs',
                'params': {},
                'event': threading.Event(),
                'result': None,
                'error': None,
                'created_at': time.time(),
                'picked_up': False,
                'target_client': 'device-a',
                'claimed_client_id': '',
                'claimed_owner_user_id': '',
                'timeout': 30,
                'cancelled': False,
                'owner_user_id': '41',
            }

    delivered = get_pending_commands(
        client_id='device-a', owner_user_id='41')
    assert len(delivered) == MAX_COMMANDS_PER_POLL
    with _state._commands_lock:
        remaining = [
            command for command in _state._commands.values()
            if not command['picked_up']
        ]
    assert len(remaining) == 8
