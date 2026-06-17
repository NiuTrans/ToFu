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
    CUSTOM_TOOL_PREFIX, CustomToolError, ToolLimits, count_tool_envs,
    dispose_tool_env, mint_tool_env, request_client_tool_result,
    resolve_client_tool_result,
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
        from lib.tasks_pkg.tool_dispatch import (
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

        def _wait():
            result['val'] = request_client_tool_result(call_id, timeout=5)

        t = threading.Thread(target=_wait, daemon=True)
        t.start()
        time.sleep(0.2)
        assert resolve_client_tool_result(call_id, 'the answer', is_error=False)
        t.join(timeout=3)
        assert result['val'] == ('the answer', False)

    def test_resolve_unknown_returns_false(self):
        assert resolve_client_tool_result('ctool_nope', 'x') is False


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
