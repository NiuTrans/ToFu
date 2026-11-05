"""tests/tools/test_tool_declared_timeout.py — Declared per-tool timeout (4b).

Pins the two halves of the declared-timeout seam:

* registration: ``ToolSpec.timeout_ms_by_name`` is validated at registration
  (undeclared names fail loud; non-int / out-of-range values fail loud), and
  ``declared_tool_timeout_ms`` resolves through the registry facade;
* execution: ``_execute_tool_one_in_pool`` arms ONLY a declared timeout, and
  only its OWN timer's expiry replaces the late result with the canonical
   typed timeout result + error status + ``_declaredTimeoutMs`` attribution. An
  outer abort keeps its own semantics; a fast handler is untouched; a
  ratified human-wait exemption never arms.
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def registry_state():
    """Snapshot/restore the process-global spec list + dispatch registry so
    probe registrations cannot leak into the shared session registry (the
    established pattern from test_tool_registry_builtin_name_protection.py).
    """
    import lib.tasks_pkg.handlers  # noqa: F401 — ensures built-in handlers exist
    from lib.tasks_pkg.executor import tool_registry
    from lib.tools.registry import _spec

    saved_specs = list(_spec._TOOL_SPECS)
    saved_keys = set(_spec._REGISTERED_KEYS)
    snap = tool_registry.snapshot()
    try:
        yield tool_registry
    finally:
        _spec._TOOL_SPECS[:] = saved_specs
        _spec._REGISTERED_KEYS.clear()
        _spec._REGISTERED_KEYS.update(saved_keys)
        tool_registry.restore(snap)


def _spec(key='probe', *, provides=('probe_tool',), timeout=None):
    from lib.tools.registry import ToolSpec
    return ToolSpec(
        key, lambda ctx: [], phase='base',
        provides=frozenset(provides),
        timeout_ms_by_name=timeout or {})


class TestDeclaredTimeoutRegistration:
    def _register(self, spec):
        from lib.tools.registry import register_tool_spec
        register_tool_spec(spec, replace=True)

    def test_undeclared_name_fails_loud(self, registry_state):
        with pytest.raises(ValueError, match='undeclared tool names'):
            self._register(_spec(timeout={'other_tool': 5_000}))

    @pytest.mark.parametrize('value', [0, -1, 500, 3_600_001, '60', 60.0, True])
    def test_invalid_values_fail_loud(self, value, registry_state):
        with pytest.raises(ValueError, match='invalid timeout_ms'):
            self._register(_spec(timeout={'probe_tool': value}))

    def test_boundary_values_accepted(self, registry_state):
        from lib.tools.registry import declared_tool_timeout_ms
        self._register(_spec(timeout={'probe_tool': 1_000}))
        assert declared_tool_timeout_ms('probe_tool') == 1_000
        self._register(_spec(timeout={'probe_tool': 3_600_000}))
        assert declared_tool_timeout_ms('probe_tool') == 3_600_000

    def test_builtin_web_tools_declare_sixty_seconds(self):
        from lib.tools.registry import declared_tool_timeout_ms
        assert declared_tool_timeout_ms('web_search') == 60_000
        assert declared_tool_timeout_ms('fetch_url') == 60_000
        assert declared_tool_timeout_ms('read_files') is None
        assert declared_tool_timeout_ms('ask_human') is None

    def test_declared_maximum_wait_fits_execution_deadline(self):
        from lib.tools.execution_deadlines import (
            declared_wait_seconds, TOOL_WAIT_COMPLETION_ALLOWANCE_MS)
        from lib.tools.coordinator import COORDINATOR_TOOLS
        from lib.tools.poll import POLL_TOOL
        from lib.tools.project import PROJECT_TOOL_CHECK_BACKGROUND
        from lib.tools.registry import all_specs, declared_tool_timeout_ms
        from lib.tools.registry._introspect import _reference_context
        ctx = _reference_context()
        from lib.swarm.tools import AWAIT_AGENTS_TOOL
        schemas = {schema['function']['name']: schema
                   for spec in all_specs() for schema in spec.build(ctx)}
        # Role-gated and replay-only schemas are absent from a fresh catalog.
        schemas.update({tool['function']['name']: tool for tool in (
            COORDINATOR_TOOLS + [POLL_TOOL, PROJECT_TOOL_CHECK_BACKGROUND, AWAIT_AGENTS_TOOL])})
        waits = {name: declared_wait_seconds(schema)
                 for name, schema in schemas.items()
                 if declared_wait_seconds(schema) is not None}
        assert set(waits) == {
            'await_project_workers', 'poll', 'await_agents', 'check_background_command'}
        for name, wait_seconds in waits.items():
            assert declared_tool_timeout_ms(
                name, tool_schemas=[schemas[name]]) == (
                    wait_seconds * 1000 + TOOL_WAIT_COMPLETION_ALLOWANCE_MS)
        assert waits == {'await_project_workers': 1800, 'poll': 1500,
                         'await_agents': 60, 'check_background_command': 60}

    def test_live_wait_arguments_use_their_own_bound(self):
        from lib.tools.registry import declared_tool_timeout_ms
        from lib.tools.coordinator import COORDINATOR_TOOLS
        from lib.tools.poll import POLL_TOOL
        tools = COORDINATOR_TOOLS + [POLL_TOOL]
        assert declared_tool_timeout_ms('await_project_workers', {}, tools) == 1_860_000
        assert declared_tool_timeout_ms('await_project_workers', {'timeout_seconds': 600}, tools) == 660_000
        assert declared_tool_timeout_ms('await_project_workers', {'timeout_seconds': 0}, tools) is None
        assert declared_tool_timeout_ms('poll', {}, tools) is None
        assert declared_tool_timeout_ms('poll', {'wait_s': 0}, tools) is None
        assert declared_tool_timeout_ms('poll', {'wait_s': 1500}, tools) == 1_560_000


def _pool_task():
    return {
        'id': 'timeout-task-1',
        'convId': 'cv-timeout',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'events': [],
        'events_lock': threading.Lock(),
    }


class TestDeclaredTimeoutExecution:
    def _execute(self, monkeypatch, *, task=None, timeout_ms=100, expire=False,
                 fn_name='slow_tool', fn_args=None, aborted_after=False):
        """Run one pooled call whose declared timer only the test expires.

        The wrapper gets a ``threading.Timer`` that is never scheduled
        (docs/TESTING_STRATEGY.md, "Scenario timeouts expire by scenario,
        not by wall clock"). ``expire`` fires the call's own timer while the
        handler is in flight; otherwise an armed handler outlasts the nominal
        interval, so a return to wall-clock expiry fails every run.
        """
        import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
        import lib.tasks_pkg.tool_runtime as tool_runtime
        task = task or _pool_task()
        round_entry = {'toolCallId': 'tc-1', 'status': 'executing'}
        armed, cancellations = [], []

        class TestTimer(threading.Timer):
            def start(self):
                armed.append(self)

        class TestThreading:
            Timer = TestTimer

            def __getattr__(self, name):
                return getattr(threading, name)

        class CallContext:
            def request_resource_cancellation(self):
                cancellations.append('tc-1')

        def _execute_one(task_, tc, fn_name_, tc_id, fn_args_, rn,
                         round_entry_, cfg, project_path, project_enabled,
                         all_tools=None):
            if expire:
                timer, = armed
                timer.function(*timer.args, **timer.kwargs)
            elif armed:
                time.sleep(5 * armed[0].interval)
            if aborted_after:
                task_['aborted'] = True
            return tc_id, 'late result bytes', False

        monkeypatch.setattr(heartbeat, 'threading', TestThreading())
        monkeypatch.setattr(heartbeat, '_execute_tool_one', _execute_one)
        monkeypatch.setattr(
            tool_runtime, 'active_context_for_call',
            lambda task_, *, round_num, tool_call_id, round_entry:
                CallContext() if (round_num, tool_call_id) == (1, 'tc-1') else None)
        import lib.tools.registry as registry
        monkeypatch.setattr(registry, 'declared_tool_timeout_ms',
                            lambda name, arguments=None, tool_schemas=None: timeout_ms)
        result = heartbeat._execute_tool_one_in_pool(
            task, {'id': 'tc-1'}, fn_name, 'tc-1', fn_args or {}, 1,
            round_entry, {}, None, False)
        return result, task, round_entry, armed, cancellations

    def test_own_timer_replaces_late_result(self, monkeypatch):
        from lib.tools.result_envelope import tool_result_error

        (tc_id, content, is_search), _task, round_entry, armed, cancellations = (
            self._execute(monkeypatch, timeout_ms=100, expire=True))
        assert [timer.interval for timer in armed] == [0.1]
        assert cancellations == ['tc-1'], 'expiry must cancel the call cooperatively'
        assert tc_id == 'tc-1'
        error = tool_result_error(content)
        assert error.code == 'tool_execution_timeout'
        assert error.message == 'Tool execution timed out: slow_tool'
        assert error.retryable is False, 'undeclared recovery must not permit replay'
        assert is_search is False
        assert round_entry['status'] == 'error'
        assert round_entry['_declaredTimeoutMs'] == 100

    def test_fast_handler_untouched(self, monkeypatch):
        (tc_id, content, _s), _task, round_entry, armed, cancellations = (
            self._execute(monkeypatch, timeout_ms=50))
        assert content == 'late result bytes'
        assert '_declaredTimeoutMs' not in round_entry
        assert [timer.interval for timer in armed] == [0.05]
        assert armed[0].finished.is_set(), 'a settled call must cancel its timer'
        assert cancellations == []

    def test_no_declaration_means_no_arming(self, monkeypatch):
        import lib.tools.registry as registry
        monkeypatch.setattr(registry, 'declared_tool_timeout_ms',
                            lambda name, arguments=None, tool_schemas=None: None)
        (tc_id, content, _s), _task, round_entry, armed, _c = self._execute(
            monkeypatch, timeout_ms=None)
        assert content == 'late result bytes'
        assert armed == []

    def test_outer_abort_keeps_its_own_semantics(self, monkeypatch):
        (tc_id, content, _s), task, round_entry, armed, _c = self._execute(
            monkeypatch, timeout_ms=50, expire=True, aborted_after=True)
        assert len(armed) == 1, 'the declared timer must have expired in flight'
        assert task['aborted'] is True
        assert content == 'late result bytes', (
            'an outer abort must not be rewritten into a timeout verdict')
        assert '_declaredTimeoutMs' not in round_entry

    def test_exempt_wait_never_arms(self, monkeypatch):
        import lib.tools.registry as registry
        consulted = []
        monkeypatch.setattr(
            registry, 'declared_tool_timeout_ms',
            lambda name, arguments=None, tool_schemas=None: consulted.append(name) or 50)
        (tc_id, content, _s), _task, _re, armed, _c = self._execute(
            monkeypatch, timeout_ms=None,
            fn_name='ask_human', fn_args={'question': 'continue?'})
        assert consulted == [], 'human-wait exemption must skip the accessor'
        assert armed == []
        assert content == 'late result bytes'
