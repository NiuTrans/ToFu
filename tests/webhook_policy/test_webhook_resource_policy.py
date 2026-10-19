"""Launch hints size reuse without webhook admission ceilings."""
import pytest
from lib.webhook_policy import resolve_webhook_budget
pytestmark = pytest.mark.unit


def test_launch_capacity_scales_file_and_worker_reuse():
    small = resolve_webhook_budget({'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY':'64', 'TOFU_AGENT_WORKERS':'4'})
    large = resolve_webhook_budget({'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY':'2048', 'TOFU_AGENT_WORKERS':'128'})
    assert large.spool_segment_bytes > small.spool_segment_bytes
    assert large.worker_reuse_target > small.worker_reuse_target


def test_retired_admission_overrides_are_ignored():
    environment = {'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY':'64', 'TOFU_AGENT_WORKERS':'4'}
    before = resolve_webhook_budget(environment)
    environment.update({key:'1' for key in ('TOFU_WEBHOOK_SUBSCRIPTION_CAPACITY',
        'TOFU_WEBHOOK_QUEUE_CAPACITY','TOFU_WEBHOOK_BUFFER_MAX_MIB',
        'TOFU_WEBHOOK_EVENT_MAX_KIB','TOFU_WEBHOOK_MAX_ATTEMPTS')})
    assert resolve_webhook_budget(environment) == before
