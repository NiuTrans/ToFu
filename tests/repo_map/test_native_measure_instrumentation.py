"""Real diagnostic hooks, including CPython monitoring, cannot masquerade as clean clocks."""
import cProfile
import sys
import threading
import tracemalloc
import pytest
from benchmarks.repository_index.native_domain_worker import _instrumentation_state

pytestmark = pytest.mark.unit


def test_actual_cprofile_is_observable_without_legacy_profile_hook():
    profile = cProfile.Profile()
    profile.enable()
    try:
        assert _instrumentation_state()['profiler_enabled'] is True
    finally:
        profile.disable()


@pytest.mark.parametrize('thread_default', [False, True])
def test_python_trace_is_observable(thread_default):
    owner = threading if thread_default else sys
    before = owner.gettrace()
    owner.settrace(lambda *_args: None)
    try:
        assert _instrumentation_state()['profiler_enabled'] is True
    finally:
        owner.settrace(before)


def test_code_local_monitoring_cannot_be_reported_as_clean():
    monitoring = getattr(sys, 'monitoring', None)
    if monitoring is None:
        pytest.skip('monitoring starts with Python 3.12')
    tool = next((i for i in range(6) if monitoring.get_tool(i) is None and not monitoring.get_events(i)), None)
    if tool is None:
        pytest.skip('all monitoring tool slots already owned')
    def monitored_function(): return 1
    monitoring.use_tool_id(tool, 'repository-measurement-test')
    try:
        monitoring.set_local_events(tool, monitored_function.__code__, monitoring.events.LINE)
        assert monitoring.get_events(tool) == 0
        assert _instrumentation_state()['profiler_enabled'] is True
    finally:
        monitoring.set_local_events(tool, monitored_function.__code__, 0)
        monitoring.free_tool_id(tool)


def test_actual_allocation_tracing_is_observable():
    existing = tracemalloc.is_tracing()
    if not existing: tracemalloc.start(1)
    try:
        assert _instrumentation_state()['tracemalloc_enabled'] is True
    finally:
        if not existing: tracemalloc.stop()
