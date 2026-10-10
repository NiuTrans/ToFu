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


def test_live_poll_burst_has_no_credential_owner_global_or_rate_quota():
    controller = _controller(max_inflight=1, credential_rpm=30, owner_rpm=30, global_rpm=30)
    leases = []
    for index in range(4096):
        allowed, lease = controller.enter(credential='same-device')
        assert allowed.allowed and lease is not None
        assert controller.admit_owner(lease, owner_user_id=41 + index % 2).allowed
        leases.append(lease)
    assert controller.snapshot()['active'] == 4096
    assert controller.snapshot()['activeOwners'] == 2
    for lease in leases:
        controller.release(lease)
        controller.release(lease)
    assert controller.snapshot()['active'] == 0


def test_settled_credentials_leave_no_rate_or_cooldown_registry():
    controller = _controller(max_bucket_entries=16)
    for index in range(1024):
        controller.note_protocol_rejection(credential=str(index), client_protocol_version=1)
        decision, lease = controller.enter(credential=str(index))
        assert decision.allowed
        controller.release(lease)
    state = controller.snapshot()
    assert state['active'] == state['activeCredentials'] == state['credentialBuckets'] == state['protocolCooldowns'] == 0


def test_poll_lease_cannot_be_rebound_forged_or_claimed_after_release():
    from lib.browser.poll_admission import BrowserPollLease
    controller = _controller()
    _, lease = controller.enter(credential='fixture')
    assert controller.admit_owner(lease, owner_user_id=41).allowed
    with pytest.raises(RuntimeError, match='cannot change'):
        controller.admit_owner(lease, owner_user_id=42)
    with pytest.raises(RuntimeError):
        _controller().admit_owner(lease, owner_user_id=41)
    with pytest.raises(RuntimeError):
        controller.release(BrowserPollLease(lease.credential_fingerprint))
    controller.release(lease)
    with pytest.raises(RuntimeError):
        controller.admit_owner(lease, owner_user_id=41)


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


def test_each_poll_authenticates_before_validating_current_protocol(
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
    assert len(calls) == 2, 'a prior protocol error cannot replace current authentication'


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


def test_single_device_poll_burst_stays_authenticated_without_local_429(flask_client, monkeypatch):
    from lib.browser.poll_admission import reset_browser_poll_admission_for_tests
    async def no_commands(**kwargs):
        return []
    monkeypatch.setattr('lib.browser.queue.wait_for_commands_async', no_commands)
    controller = _controller(credential_rpm=30, owner_rpm=30, global_rpm=30)
    reset_browser_poll_admission_for_tests(controller)
    token = _token(name='burst-device')
    for _ in range(128):
        assert _poll(flask_client, token, 'burst-device').status_code == 200
    assert controller.snapshot()['active'] == 0


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


def test_client_registry_retains_live_devices_without_owner_or_global_count_quota():
    from lib.browser.queue import mark_poll
    from lib.browser.queue import _state
    for index in range(256):
        mark_poll(f'device-{index}', owner_user_id='41', protocol_version=2, capabilities=[])
    with _state._clients_lock:
        assert len(_state._clients) == 256
        assert all(row['owner_user_id'] == '41' for row in _state._clients.values())


def test_unique_poll_waiters_do_not_refuse_or_drop_live_waiters():
    import lib.browser.queue._dispatch as dispatch
    from lib.browser.queue import _state
    async def exercise():
        polls = [asyncio.create_task(dispatch.wait_for_commands_async(
            timeout=5, client_id=f'device-{index}', owner_user_id='41')) for index in range(128)]
        try:
            await asyncio.sleep(.05)
            with _state._async_waiters_lock:
                assert len(_state._async_waiters) == 128
            for poll in polls:
                poll.cancel()
            outcomes = await asyncio.gather(*polls, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in outcomes)
            with _state._async_waiters_lock:
                assert not _state._async_waiters
        finally:
            for poll in polls:
                poll.cancel()
            await asyncio.gather(*polls, return_exceptions=True)
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
