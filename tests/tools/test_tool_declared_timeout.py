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
    def _execute(self, monkeypatch, *, task=None, timeout_ms=100, delay=0.4,
                 fn_name='slow_tool', fn_args=None, aborted_after=False):
        import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
        task = task or _pool_task()
        round_entry = {'toolCallId': 'tc-1', 'status': 'executing'}

        def _slow_execute(task_, tc, fn_name_, tc_id, fn_args_, rn,
                          round_entry_, cfg, project_path, project_enabled,
                          all_tools=None):
            time.sleep(delay)
            if aborted_after:
                task_['aborted'] = True
            return tc_id, 'late result bytes', False

        monkeypatch.setattr(heartbeat, '_execute_tool_one', _slow_execute)
        if timeout_ms is not None:
            import lib.tools.registry as registry
            monkeypatch.setattr(registry, 'declared_tool_timeout_ms',
                                lambda name: timeout_ms)
        return heartbeat._execute_tool_one_in_pool(
            task, {'id': 'tc-1'}, fn_name, 'tc-1', fn_args or {}, 1,
            round_entry, {}, None, False), task, round_entry

    def test_own_timer_replaces_late_result(self, monkeypatch):
        from lib.tools.result_envelope import tool_result_error

        (tc_id, content, is_search), _task, round_entry = self._execute(
            monkeypatch, timeout_ms=100, delay=0.4)
        assert tc_id == 'tc-1'
        error = tool_result_error(content)
        assert error.code == 'tool_execution_timeout'
        assert error.message == 'Tool execution timed out: slow_tool'
        assert error.retryable is False, 'undeclared recovery must not permit replay'
        assert is_search is False
        assert round_entry['status'] == 'error'
        assert round_entry['_declaredTimeoutMs'] == 100

    def test_fast_handler_untouched(self, monkeypatch):
        (tc_id, content, _s), _task, round_entry = self._execute(
            monkeypatch, timeout_ms=500, delay=0.05)
        assert content == 'late result bytes'
        assert '_declaredTimeoutMs' not in round_entry

    def test_no_declaration_means_no_arming(self, monkeypatch):
        import lib.tools.registry as registry
        monkeypatch.setattr(registry, 'declared_tool_timeout_ms',
                            lambda name: None)
        (tc_id, content, _s), _task, round_entry = self._execute(
            monkeypatch, timeout_ms=None, delay=0.05)
        assert content == 'late result bytes'

    def test_outer_abort_keeps_its_own_semantics(self, monkeypatch):
        (tc_id, content, _s), task, round_entry = self._execute(
            monkeypatch, timeout_ms=50, delay=0.2, aborted_after=True)
        assert task['aborted'] is True
        assert content == 'late result bytes', (
            'an outer abort must not be rewritten into a timeout verdict')
        assert '_declaredTimeoutMs' not in round_entry

    def test_exempt_wait_never_arms(self, monkeypatch):
        import lib.tools.registry as registry
        consulted = []
        monkeypatch.setattr(
            registry, 'declared_tool_timeout_ms',
            lambda name: consulted.append(name) or 50)
        (tc_id, content, _s), _task, _re = self._execute(
            monkeypatch, timeout_ms=None, delay=0.05,
            fn_name='ask_human', fn_args={'question': 'continue?'})
        assert consulted == [], 'human-wait exemption must skip the accessor'
        assert content == 'late result bytes'
