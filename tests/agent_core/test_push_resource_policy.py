"""Push reuse hints scale without an admission envelope."""
import pytest
from lib.agent_core.push_policy import resolve_push_budget
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
        'TOFU_PUSH_EVENT_QUEUE_CAPACITY', 'TOFU_PUSH_EVENT_QUEUE_MAX_MIB',
        'TOFU_PUSH_EVENT_MAX_MIB', 'TOFU_PUSH_CLIENT_CAPACITY', 'TOFU_PUSH_OWNER_CLIENT_CAPACITY')})
    assert resolve_push_budget(environment) == before
