"""tests/test_custom_tool_isolation.py — Per-request custom tool guards.

Verifies the contamination contract from docs/CUSTOM_TOOLS.md:

* validation rejects bad names / built-in collisions / over-cap / disabled
  sandbox, and strips server-only keys from the LLM-facing schema;
* minting + disposing an env leaves the GLOBAL tool_registry byte-identical;
* two concurrent envs each resolve to their OWN handler (no cross-leak);
* the dispatch-time write/idempotent partition unions the env's flags;
* the client-handoff request/resolve round-trips;
* AST guard: no /api/v1 request module imports/calls register_tool_spec or
  tool_registry.register (the global-mutation forbidden surface).
"""

from __future__ import annotations

import ast
import os
import threading
import time

import pytest

from lib.tools.tool_env import (
    CUSTOM_TOOL_PREFIX, MAX_CUSTOM_TOOL_CALL_ID_CHARS, CustomToolError,
    ToolLimits, count_tool_envs, dispose_tool_env, mint_tool_env,
    request_client_tool_result, resolve_client_tool_result,
)


def _fn(name, mode='client', **extra):
    tool = {'type': 'function',
            'function': {'name': name, 'description': 'x',
                         'parameters': {'type': 'object', 'properties': {}}}}
    if mode is not None:
        tool['execution'] = {'mode': mode, **extra.pop('execution', {})}
    tool.update(extra)
    return tool


# ── Validation ──────────────────────────────────────────────────────

class TestValidation:
    def test_rejects_non_prefixed_name(self):
        with pytest.raises(CustomToolError):
            mint_tool_env(tools=[_fn('get_weather')])

    def test_rejects_builtin_collision(self):
        # write_file is a built-in; even with the prefix a collision is checked,
        # but the prefix itself already prevents shadowing. Assert a prefixed
        # name that maps to a built-in base is still safe to mint (no collision)
        # and that a bare built-in name is rejected by the prefix rule.
        with pytest.raises(CustomToolError):
            mint_tool_env(tools=[_fn('write_file')])

    def test_prefixed_name_never_collides_with_builtin(self):
        env = mint_tool_env(tools=[_fn('custom__write_file')])
        try:
            assert env.tools[0].name == 'custom__write_file'
        finally:
            dispose_tool_env(env)

    def test_enforces_max_tools(self):
        lim = ToolLimits(max_tools=2)
        with pytest.raises(CustomToolError):
            mint_tool_env(tools=[_fn(f'custom__t{i}') for i in range(3)],
                          limits=lim)

    def test_rejects_duplicate_names(self):
        with pytest.raises(CustomToolError):
            mint_tool_env(tools=[_fn('custom__a'), _fn('custom__a')])

    def test_schema_strips_server_only_keys(self):
        env = mint_tool_env(tools=[_fn('custom__a', write=True, idempotent=True)])
        try:
            s = env.schemas[0]
            assert set(s.keys()) == {'type', 'function'}
            assert 'execution' not in s and 'write' not in s and 'idempotent' not in s
        finally:
            dispose_tool_env(env)

    def test_webhook_requires_url(self):
        with pytest.raises(CustomToolError):
            mint_tool_env(tools=[_fn('custom__w', mode='webhook')])

    def test_sandbox_disabled_by_default(self):
        os.environ.pop('TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX', None)
        with pytest.raises(CustomToolError):
            mint_tool_env(tools=[_fn('custom__s', mode='sandbox',
                                     execution={'command': 'echo hi'})])

    def test_sandbox_allowed_when_operator_opts_in(self):
        os.environ['TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX'] = '1'
        try:
            env = mint_tool_env(tools=[_fn('custom__s', mode='sandbox',
                                           execution={'command': 'echo hi'})])
            assert env.tools[0].mode == 'sandbox'
            dispose_tool_env(env)
        finally:
            os.environ.pop('TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX', None)


# ── Global registry is never mutated ────────────────────────────────

class TestGlobalRegistryUntouched:
    def _registry_snapshot(self):
        from lib.tasks_pkg.executor import tool_registry
        # Capture the full keyspace + handler identities.
        exact = dict(tool_registry._exact)
        special = dict(tool_registry._special)
        sets = [(frozenset(s), h) for s, h in tool_registry._sets]
        return exact, special, sets

    def test_mint_and_dispose_leave_registry_identical(self):
        before = self._registry_snapshot()
        env = mint_tool_env(tools=[_fn('custom__x'), _fn('custom__y')])
        mid = self._registry_snapshot()
        dispose_tool_env(env)
        after = self._registry_snapshot()
        assert before == mid == after, (
            'global tool_registry changed when minting/disposing a custom env')

    def test_dispose_is_idempotent(self):
        env = mint_tool_env(tools=[_fn('custom__x')])
        assert dispose_tool_env(env) is True
        assert dispose_tool_env(env) is False


# ── Two concurrent envs resolve to their own handlers ───────────────

class TestPerRequestIsolation:
    def test_two_envs_resolve_independently(self):
        os.environ['TOFU_BYO_ALLOW_HOSTS'] = 'a.example.com,b.example.com'
        try:
            env_a = mint_tool_env(tools=[_fn('custom__shared', mode='webhook',
                                             execution={'url': 'https://a.example.com'})])
            env_b = mint_tool_env(tools=[_fn('custom__shared', mode='webhook',
                                             execution={'url': 'https://b.example.com'})])
        finally:
            os.environ.pop('TOFU_BYO_ALLOW_HOSTS', None)
        try:
            ha = env_a.resolve('custom__shared')
            hb = env_b.resolve('custom__shared')
            assert ha is not None and hb is not None
            assert ha is not hb
            # Each env knows only its own tool.
            assert env_a._get('custom__shared').execution['url'] == 'https://a.example.com'
            assert env_b._get('custom__shared').execution['url'] == 'https://b.example.com'
            assert env_a.resolve('custom__nope') is None
        finally:
            dispose_tool_env(env_a)
            dispose_tool_env(env_b)

    def test_env_count_tracks_live_envs(self):
        base = count_tool_envs()
        env = mint_tool_env(tools=[_fn('custom__x')])
        assert count_tool_envs() == base + 1
        dispose_tool_env(env)
        assert count_tool_envs() == base


# ── Dispatch-time partition union ───────────────────────────────────

class TestPartitionUnion:
    def test_task_partitions_union_env_flags(self):
        from lib.tasks_pkg.tool_dispatch._flags import (
            _IDEMPOTENT_TOOLS, _WRITE_TOOLS, _task_partitions,
        )
        os.environ['TOFU_BYO_ALLOW_HOSTS'] = 'x.example.com'
        try:
            env = mint_tool_env(tools=[
                _fn('custom__w', mode='webhook',
                    execution={'url': 'https://x.example.com'}, write=True),
                _fn('custom__r', mode='webhook',
                    execution={'url': 'https://x.example.com'}, idempotent=True),
            ])
        finally:
            os.environ.pop('TOFU_BYO_ALLOW_HOSTS', None)
        try:
            task = {'_tool_env': env}
            write, idem = _task_partitions(task)
            assert 'custom__w' in write
            assert 'custom__r' in idem
            # Base sets are preserved.
            assert _WRITE_TOOLS <= write
            assert _IDEMPOTENT_TOOLS <= idem
            # A task without an env gets the base sets verbatim.
            assert _task_partitions({}) == (_WRITE_TOOLS, _IDEMPOTENT_TOOLS)
        finally:
            dispose_tool_env(env)


# ── Client handoff round-trip ───────────────────────────────────────

class TestClientHandoff:
    def test_request_then_resolve_unblocks(self):
        call_id = 'ctool_test123'
        result = {}
        task = {'id': 'task-owner-1', '_userId': 1}

        def _wait():
            result['val'] = request_client_tool_result(
                call_id, task=task, timeout=5)

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        time.sleep(0.2)
        assert resolve_client_tool_result(
            call_id, 'the answer', task_id='task-owner-1', user_id=1,
            is_error=False)
        t.join(timeout=3)
        assert result['val'] == ('the answer', False)

    def test_resolve_unknown_returns_false(self):
        assert resolve_client_tool_result(
            'ctool_nope', 'x', task_id='task-owner-1', user_id=1) is False

    def test_rejects_oversized_call_id_before_registration(self):
        with pytest.raises(ValueError, match='call id too long'):
            request_client_tool_result(
                'x' * (MAX_CUSTOM_TOOL_CALL_ID_CHARS + 1),
                task={'id': 'task-call-id-bound', '_userId': 1},
                timeout=1,
            )

    def test_bounds_result_before_waiter_consumes_it(self):
        call_id = 'call_result_bound'
        task = {'id': 'task-result-bound', '_userId': 1}
        env = mint_tool_env(
            tools=[_fn('custom__bounded')],
            limits=ToolLimits(max_result_chars=8),
        )
        task['_tool_env'] = env
        result = {}
        waiter = threading.Thread(
            target=lambda: result.setdefault(
                'val', request_client_tool_result(
                    call_id, task=task, timeout=5)),
            daemon=True,
        )
        try:
            waiter.start()
            time.sleep(0.2)
            assert resolve_client_tool_result(
                call_id, '0123456789abcdef', task_id=task['id'], user_id=1)
            waiter.join(timeout=3)
            assert result['val'] == (
                '01234567\n… [custom tool result truncated]', False)
        finally:
            dispose_tool_env(env)

    def test_capacity_rejects_new_waiter_without_displacing_existing(
            self, monkeypatch):
        monkeypatch.setattr('lib.tools.tool_env._MAX_CLIENT_RESULTS', 1)
        first = {'id': 'task-capacity-one', '_userId': 1}
        second = {'id': 'task-capacity-two', '_userId': 2}
        result = {}
        waiter = threading.Thread(
            target=lambda: result.setdefault(
                'first', request_client_tool_result(
                    'call_one', task=first, timeout=5)),
            daemon=True,
        )
        waiter.start()
        time.sleep(0.2)

        rejected = request_client_tool_result(
            'call_two', task=second, timeout=1)
        assert rejected[1] is True
        assert 'capacity is temporarily full' in rejected[0]
        assert resolve_client_tool_result(
            'call_one', 'owned', task_id=first['id'], user_id=1)
        waiter.join(timeout=3)
        assert result['first'] == ('owned', False)

    def test_foreign_task_cannot_resolve_handoff(self):
        call_id = 'ctool_owner_fence'
        task = {'id': 'task-owner-1', '_userId': 1}
        result = {}

        waiter = threading.Thread(
            target=lambda: result.setdefault(
                'val', request_client_tool_result(
                    call_id, task=task, timeout=5)),
            daemon=True,
        )
        waiter.start()
        time.sleep(0.2)
        assert resolve_client_tool_result(
            call_id, 'stolen', task_id='task-owner-2', user_id=2) is False
        assert resolve_client_tool_result(
            call_id, 'owned', task_id='task-owner-1', user_id=1) is True
        waiter.join(timeout=3)
        assert result['val'] == ('owned', False)

    def test_same_call_id_is_isolated_between_concurrent_tasks(self):
        call_id = 'call_0'
        results = {}
        tasks = {
            'one': {'id': 'task-concurrent-1', '_userId': 1},
            'two': {'id': 'task-concurrent-2', '_userId': 2},
        }

        waiters = [
            threading.Thread(
                target=lambda label=label: results.setdefault(
                    label, request_client_tool_result(
                        call_id, task=tasks[label], timeout=5)),
                daemon=True,
            )
            for label in tasks
        ]
        for waiter in waiters:
            waiter.start()
        time.sleep(0.2)

        assert resolve_client_tool_result(
            call_id, 'result two', task_id='task-concurrent-2', user_id=2)
        assert resolve_client_tool_result(
            call_id, 'result one', task_id='task-concurrent-1', user_id=1)
        for waiter in waiters:
            waiter.join(timeout=3)

        assert results == {
            'one': ('result one', False),
            'two': ('result two', False),
        }

    def test_duplicate_pending_id_in_one_task_fails_without_replacing_waiter(self):
        call_id = 'call_duplicate'
        task = {'id': 'task-duplicate', '_userId': 1}
        result = {}
        waiter = threading.Thread(
            target=lambda: result.setdefault(
                'first', request_client_tool_result(
                    call_id, task=task, timeout=5)),
            daemon=True,
        )
        waiter.start()
        time.sleep(0.2)

        duplicate = request_client_tool_result(call_id, task=task, timeout=5)
        assert duplicate[1] is True
        assert 'already pending' in duplicate[0]
        assert resolve_client_tool_result(
            call_id, 'first result', task_id='task-duplicate', user_id=1)
        waiter.join(timeout=3)
        assert result['first'] == ('first result', False)

    def test_dispose_unblocks_only_its_own_pending_handoffs(self):
        env_one = mint_tool_env(
            tools=[_fn('custom__one')], owner='owner:1')
        env_two = mint_tool_env(
            tools=[_fn('custom__two')], owner='owner:2')
        tasks = {
            'one': {'id': 'task-dispose-1', '_userId': 1,
                    '_tool_env': env_one},
            'two': {'id': 'task-dispose-2', '_userId': 2,
                    '_tool_env': env_two},
        }
        results = {}
        waiters = [
            threading.Thread(
                target=lambda label=label: results.setdefault(
                    label, request_client_tool_result(
                        'call_0', task=tasks[label], timeout=5)),
                daemon=True,
            )
            for label in tasks
        ]
        try:
            for waiter in waiters:
                waiter.start()
            time.sleep(0.2)

            assert dispose_tool_env(env_one) is True
            waiters[0].join(timeout=3)
            assert results['one'][1] is True
            assert 'disposed' in results['one'][0]
            assert 'two' not in results

            assert resolve_client_tool_result(
                'call_0', 'still owned', task_id='task-dispose-2', user_id=2)
            waiters[1].join(timeout=3)
            assert results['two'] == ('still owned', False)
        finally:
            dispose_tool_env(env_one)
            dispose_tool_env(env_two)

    def test_client_response_wins_exact_timeout_boundary(self, monkeypatch):
        call_id = 'call_timeout_boundary'
        task = {'id': 'task-timeout-boundary', '_userId': 1}

        class CrossingEvent:
            def __init__(self):
                self.set_called = False

            def set(self):
                self.set_called = True

            def wait(self, timeout):
                assert resolve_client_tool_result(
                    call_id, 'accepted', task_id=task['id'], user_id=1)
                return False

        clock = iter((0.0, 0.0, 2.0))
        monkeypatch.setattr('lib.tools.tool_env.threading.Event', CrossingEvent)
        monkeypatch.setattr('lib.tools.tool_env.time.time', lambda: next(clock))

        assert request_client_tool_result(
            call_id, task=task, timeout=1) == ('accepted', False)


# ── AST guard: request modules must not mutate the global registry ──

class TestNoGlobalMutationFromRoutes:
    FORBIDDEN = {'register_tool_spec'}

    def _api_v1_dir(self):
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.normpath(os.path.join(here, '..', 'routes', 'api_v1'))

    def test_no_api_v1_module_registers_tools_globally(self):
        offenders = []
        root = self._api_v1_dir()
        for fname in os.listdir(root):
            if not fname.endswith('.py'):
                continue
            path = os.path.join(root, fname)
            with open(path, 'r', encoding='utf-8') as f:
                src = f.read()
            tree = ast.parse(src, filename=path)
            for node in ast.walk(tree):
                # import of register_tool_spec
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in self.FORBIDDEN:
                            offenders.append(f'{fname}: imports {alias.name}')
                # call to tool_registry.register(...)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if (node.func.attr in ('register', 'register_set',
                                            'register_special')
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == 'tool_registry'):
                        offenders.append(f'{fname}: calls tool_registry.{node.func.attr}')
        if offenders:
            pytest.fail(
                'A request handler mutates the GLOBAL tool registry — custom '
                'tools must go through task["_tool_env"], never the singleton:\n  '
                + '\n  '.join(offenders))


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
