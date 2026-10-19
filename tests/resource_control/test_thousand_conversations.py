"""Large I/O workloads can grow past historical counters, within RSS limits."""
import logging

import pytest

from lib.resource_control.runtime import launch_policy
from lib.server.server_loop_runtime import _worker_count
from runtime_guards import task_concurrency_hard_ceiling

pytestmark = pytest.mark.unit


def test_all_execution_owners_admit_a_thousand_when_the_memory_budget_fits():
    environment = {'TOFU_AGENT_WORKERS': '1200', 'TOFU_MAX_INFLIGHT_TASKS': '1200',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '131072', 'TOFU_TASK_RSS_RESERVE_MB': '64'}
    assert task_concurrency_hard_ceiling(environment) == 1536
    assert _worker_count('TOFU_AGENT_WORKERS', environment, logging.getLogger(__name__)) == 1200
    policy = launch_policy(environment)
    assert policy['root_maximum'] == 1200
    assert 'route_capacity' not in policy


def test_large_explicit_worker_count_cannot_escape_the_memory_budget():
    environment = {'TOFU_AGENT_WORKERS': '1200', 'TOFU_MAX_INFLIGHT_TASKS': '1200',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '4096', 'TOFU_TASK_RSS_RESERVE_MB': '1024'}
    assert _worker_count('TOFU_AGENT_WORKERS', environment, logging.getLogger(__name__)) == 3
    assert launch_policy(environment)['root_maximum'] == 3
