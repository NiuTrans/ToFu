"""Push reuse hints scale without an admission envelope."""
import asyncio

import pytest
from lib.agent_core.push_policy import (
    resolve_push_budget,
    resolve_push_event_queue_capacity,
)
pytestmark = pytest.mark.unit


def test_push_file_and_idle_reuse_hints_follow_launch_capacity():
    small = resolve_push_budget({'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': '128', 'TOFU_AGENT_WORKERS': '4'})
    large = resolve_push_budget({'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': '2048', 'TOFU_AGENT_WORKERS': '128'})
    assert large.spool_segment_bytes > small.spool_segment_bytes
    assert large.worker_reuse_target > small.worker_reuse_target


def test_retired_event_limit_knobs_do_not_change_reuse_policy():
    environment = {'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY': '128', 'TOFU_AGENT_WORKERS': '4'}
    before = resolve_push_budget(environment)
    environment.update({key: '1' for key in (
        'TOFU_PUSH_EVENT_QUEUE_MAX_MIB',
        'TOFU_PUSH_EVENT_MAX_MIB', 'TOFU_PUSH_CLIENT_CAPACITY', 'TOFU_PUSH_OWNER_CLIENT_CAPACITY')})
    assert resolve_push_budget(environment) == before


def test_push_event_queue_capacity_scales_with_launch_probe():
    small = resolve_push_event_queue_capacity({'TOFU_AGENT_WORKERS': '2'})
    large = resolve_push_event_queue_capacity({'TOFU_AGENT_WORKERS': '128'})
    assert small == 8192
    assert large == 16384


def test_push_event_queue_explicit_override_is_bounded():
    assert resolve_push_event_queue_capacity(
        {'TOFU_PUSH_EVENT_QUEUE_CAPACITY': '1'}) == 64
    assert resolve_push_event_queue_capacity(
        {'TOFU_PUSH_EVENT_QUEUE_CAPACITY': '1000'}) == 1000
    assert resolve_push_event_queue_capacity(
        {'TOFU_PUSH_EVENT_QUEUE_CAPACITY': '999999'}) == 16384


def test_push_client_disconnects_stalled_socket_on_full_queue(monkeypatch):
    from lib.agent_core.push import PushClient

    monkeypatch.setenv('TOFU_PUSH_EVENT_QUEUE_CAPACITY', '64')
    client = PushClient(user_id=1, req_id='rid-stall')
    capacity = client._queue.maxsize
    assert capacity == 64
    for index in range(capacity):
        client.enqueue({'channel': 'chat', 'taskId': 't', 'seq': index})
    assert client._queue.full()
    assert client._connected
    client.enqueue({'channel': 'chat', 'taskId': 't', 'seq': capacity})
    assert not client._connected
    assert client._queue.empty()
