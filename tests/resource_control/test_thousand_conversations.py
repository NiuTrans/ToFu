"""A thousand conversations fit every execution owner; memory never caps them."""
import logging

import pytest

from lib.resource_control.runtime import launch_policy
from lib.server.server_loop_runtime import _worker_count
from runtime_guards import MAX_TASK_CONCURRENCY, task_concurrency_hard_ceiling

pytestmark = pytest.mark.unit


def test_all_execution_owners_admit_a_thousand_conversations():
    environment = {'TOFU_AGENT_WORKERS': '1200', 'TOFU_MAX_INFLIGHT_TASKS': '1200'}
    assert task_concurrency_hard_ceiling(environment) == MAX_TASK_CONCURRENCY
    assert _worker_count('TOFU_AGENT_WORKERS', environment, logging.getLogger(__name__)) == 1200
    policy = launch_policy(environment)
    assert policy['root_maximum'] >= 1200
    assert 'route_capacity' not in policy


def test_retired_rss_knobs_cannot_shrink_an_explicit_worker_count():
    environment = {'TOFU_AGENT_WORKERS': '1200', 'TOFU_MAX_INFLIGHT_TASKS': '1200',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '4096', 'TOFU_TASK_RSS_RESERVE_MB': '1024'}
    assert _worker_count('TOFU_AGENT_WORKERS', environment, logging.getLogger(__name__)) == 1200
    assert launch_policy(environment)['root_maximum'] >= 1200
