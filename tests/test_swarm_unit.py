"""Unit tests for the multi-agent swarm system.

Migrated from debug/test_swarm.py and debug/test_swarm_bugs.py.
Tests protocol dataclasses, registry, agent construction, master orchestrator,
and regression tests for BUG-1 (get_status), BUG-2 (AgentMessage.from_dict),
BUG-A (dependency injection), BUG-B (abort_check=None), BUG-C (results type).
"""

from unittest.mock import MagicMock, patch

import pytest

from lib.swarm.agent import SubAgent, SubAgentResult
from lib.swarm.master import MasterOrchestrator, resolve_execution_order
from lib.swarm.protocol import (
    MAX_COMPRESSED_RESULT_CHARS,
    AgentMessage,
    SubAgentStatus,
    SubTaskSpec,
    compress_result,
    format_sub_results_for_master,
)
from lib.swarm.registry import (
    AGENT_ROLES,
    get_role_config,
    get_tools_for_role,
    scope_tools_for_role,
)
from lib.swarm.tools import SPAWN_AGENTS_TOOL, SWARM_TOOL_NAMES

# ═══════════════════════════════════════════════════════════
#  Protocol Tests
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSubTaskSpec:
    def test_create_basic(self):
        spec = SubTaskSpec(id='a1', role='researcher', objective='find X')
        assert spec.id == 'a1'
        assert spec.role == 'researcher'
        assert spec.objective == 'find X'
        assert spec.depends_on == []

    def test_with_dependencies(self):
        spec = SubTaskSpec(id='b', role='coder', objective='code Y', depends_on=['a1'])
        assert spec.depends_on == ['a1']

    def test_to_dict_from_dict_roundtrip(self):
        spec = SubTaskSpec(id='c', role='writer', objective='write Z', depends_on=['a1', 'b'])
        d = spec.to_dict()
        spec2 = SubTaskSpec.from_dict(d)
        assert spec2.id == 'c'
        assert spec2.depends_on == ['a1', 'b']

    def test_from_dict_ignores_unknown(self):
        d = {'id': 'x', 'role': 'coder', 'objective': 'hi', 'random_extra_field': True}
        spec = SubTaskSpec.from_dict(d)
        assert spec.id == 'x'


@pytest.mark.unit
class TestAgentMessage:
    """Test AgentMessage dataclass (updated API with sender_id/receiver_id)."""

    def test_create(self):
        msg = AgentMessage(sender_id='a1', receiver_id='master', content='done')
        assert msg.sender_id == 'a1'
        assert msg.receiver_id == 'master'
        assert msg.content == 'done'

    def test_to_dict(self):
        msg = AgentMessage(sender_id='a1', receiver_id='master', content='done')
        d = msg.to_dict()
        assert d['sender_id'] == 'a1'
        assert 'timestamp' in d

    def test_msg_type_default(self):
        msg = AgentMessage(content='hello')
        assert msg.msg_type == 'text'

    def test_msg_type_custom(self):
        msg = AgentMessage(content='go', msg_type='instruction')
        assert msg.msg_type == 'instruction'

    def test_backward_compat_aliases(self):
        msg = AgentMessage(from_agent='x', to_agent='y', content='hello')
        assert msg.from_agent == 'x'
        assert msg.to_agent == 'y'

    def test_to_dict_roundtrip_fields(self):
        original = AgentMessage(sender_id='x', receiver_id='y', content='hello', msg_type='result')
        d = original.to_dict()
        assert d['sender_id'] == 'x'
        assert d['receiver_id'] == 'y'
        assert d['content'] == 'hello'
        assert d['msg_type'] == 'result'


@pytest.mark.unit
class TestSubAgentStatus:
    def test_values(self):
        assert SubAgentStatus.PENDING.value == 'pending'
        assert SubAgentStatus.RUNNING.value == 'running'
        assert SubAgentStatus.COMPLETED.value == 'completed'
        assert SubAgentStatus.FAILED.value == 'failed'


@pytest.mark.unit
class TestCompressResult:
    def test_short_passthrough(self):
        assert compress_result('hello') == 'hello'

    def test_empty_input(self):
        assert compress_result('') == '(no result)'

    def test_long_input_compressed(self):
        big = 'X' * (MAX_COMPRESSED_RESULT_CHARS + 1000)
        r = compress_result(big)
        assert len(r) <= MAX_COMPRESSED_RESULT_CHARS + 100


@pytest.mark.unit
class TestFormatResultsForMaster:
    def test_completed_result(self):
        spec = SubTaskSpec(id='spec-a', role='researcher', objective='find X')
        r = SubAgentResult()
        r.status = 'completed'
        r.final_answer = 'The answer is 42.'
        r.rounds_used = 3
        r.total_tokens = 1000
        txt = format_sub_results_for_master([(spec, r)])
        assert 'researcher' in txt
        assert 'find X' in txt
        assert '42' in txt

    def test_failed_result(self):
        spec = SubTaskSpec(id='spec-b', role='coder', objective='code Y')
        r = SubAgentResult()
        r.status = 'failed'
        r.error_message = 'something broke'
        txt = format_sub_results_for_master([(spec, r)])
        assert 'failed' in txt
        assert 'something broke' in txt


# ═══════════════════════════════════════════════════════════
#  Registry Tests
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRegistry:
    def test_roles_not_empty(self):
        assert len(AGENT_ROLES) > 0

    def test_general_role_exists(self):
        role = get_role_config('general')
        assert isinstance(role, dict)
        assert 'system_prompt_suffix' in role

    def test_unknown_role_falls_back(self):
        role = get_role_config('nonexistent_role_xyz')
        general = get_role_config('general')
        assert role == general

    def test_all_roles_have_required_keys(self):
        for name, role in AGENT_ROLES.items():
            assert 'system_prompt_suffix' in role, f'{name} missing system_prompt_suffix'
            assert 'tools_hint' in role, f'{name} missing tools_hint'
            assert isinstance(role['tools_hint'], list)

    def test_scope_tools_for_role_returns_list(self):
        tools = [{'function': {'name': 'web_search'}}, {'function': {'name': 'fetch_url'}}, {'function': {'name': 'run_command'}}]
        filtered = scope_tools_for_role('researcher', tools)
        assert isinstance(filtered, list)

    def test_get_tools_for_role(self):
        cats = get_tools_for_role('researcher')
        assert isinstance(cats, list)


# ═══════════════════════════════════════════════════════════
#  SubAgent Tests
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSubAgentResult:
    def test_defaults(self):
        r = SubAgentResult()
        assert r.status == 'pending'
        assert r.final_answer == ''
        assert r.error_message == ''
        assert r.rounds_used == 0
        assert r.total_tokens == 0
        assert r.cost_usd == 0.0
        # artifacts is a dict in the current API
        assert r.artifacts == {} or r.artifacts == []


def _make_agent(**overrides):
    defaults = dict(
        spec=SubTaskSpec(id='test-1', role='researcher', objective='do research'),
        parent_task={'conv_id': 'c1', 'task_id': 't1'},
        all_tools=[],
        system_prompt_base='You are helpful.',
        model='test-model',
    )
    defaults.update(overrides)
    return SubAgent(**defaults)


@pytest.mark.unit
class TestSubAgentInit:
    def test_basic_creation(self):
        agent = _make_agent()
        assert agent.spec.id == 'test-1'
        assert agent.model == 'test-model'
        assert agent.result.status == 'pending'
        assert agent.agent_id.startswith('agent-')

    def test_max_rounds_exists(self):
        agent = _make_agent()
        assert hasattr(agent, 'max_rounds')

    def test_result_is_subagentresult(self):
        agent = _make_agent(spec=SubTaskSpec(id='y', role='coder', objective='code'))
        assert isinstance(agent.result, SubAgentResult)


# ═══════════════════════════════════════════════════════════
#  Master Orchestrator Tests
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestResolveExecutionOrder:
    def test_no_deps(self):
        specs = [
            SubTaskSpec(id='a', role='researcher', objective='x'),
            SubTaskSpec(id='b', role='coder', objective='y'),
        ]
        order = resolve_execution_order(specs)
        assert isinstance(order, list)
        assert len(order) > 0

    def test_chain_dependency(self):
        specs = [
            SubTaskSpec(id='a', role='researcher', objective='find'),
            SubTaskSpec(id='b', role='coder', objective='code', depends_on=['a']),
            SubTaskSpec(id='c', role='writer', objective='write', depends_on=['b']),
        ]
        order = resolve_execution_order(specs)
        flat = [s.id for wave in order for s in wave]
        assert flat.index('a') < flat.index('b')
        assert flat.index('b') < flat.index('c')

    def test_parallel_deps(self):
        specs = [
            SubTaskSpec(id='a', role='researcher', objective='1'),
            SubTaskSpec(id='b', role='researcher', objective='2'),
            SubTaskSpec(id='c', role='writer', objective='merge', depends_on=['a', 'b']),
        ]
        order = resolve_execution_order(specs)
        flat = [s.id for wave in order for s in wave]
        assert flat.index('a') < flat.index('c')
        assert flat.index('b') < flat.index('c')

    def test_depends_on_none(self):
        spec = SubTaskSpec(id='a', role='coder', objective='test')
        spec.depends_on = None
        waves = resolve_execution_order([spec])
        assert len(waves) == 1
        assert waves[0][0].id == 'a'

    def test_empty_specs(self):
        assert resolve_execution_order([]) == []

    def test_diamond_dependency(self):
        a = SubTaskSpec(id='a', objective='root')
        b = SubTaskSpec(id='b', objective='left', depends_on=['a'])
        c = SubTaskSpec(id='c', objective='right', depends_on=['a'])
        d = SubTaskSpec(id='d', objective='merge', depends_on=['b', 'c'])
        waves = resolve_execution_order([a, b, c, d])
        assert len(waves) == 3
        assert waves[0][0].id == 'a'
        wave2_ids = {s.id for s in waves[1]}
        assert wave2_ids == {'b', 'c'}
        assert waves[2][0].id == 'd'


@pytest.mark.unit
class TestMasterOrchestratorGetStatus:
    """★ BUG-1 Regression Tests ★"""

    def _make_orchestrator(self, specs):
        return MasterOrchestrator(task_id='t1', conv_id='c1', specs=specs, model='test-model')

    def test_pending_status_before_run(self):
        specs = [SubTaskSpec(id='a', role='researcher', objective='find')]
        orch = self._make_orchestrator(specs)
        status = orch.get_status()
        assert status['a']['status'] == 'pending'

    def test_regression_bug1_live_agent_status(self):
        specs = [SubTaskSpec(id='a', role='researcher', objective='find')]
        orch = self._make_orchestrator(specs)
        agent = _make_agent(spec=specs[0])
        agent.result.status = SubAgentStatus.RUNNING.value
        agent.result.rounds_used = 3
        orch._agents[specs[0].id] = agent
        status = orch.get_status()
        assert status['a']['status'] == 'running', 'BUG-1 regression'
        assert status['a']['round'] == 3, 'BUG-1 regression'

    def test_regression_bug1_completed_agent(self):
        specs = [SubTaskSpec(id='b', role='coder', objective='code')]
        orch = self._make_orchestrator(specs)
        agent = _make_agent(spec=specs[0])
        agent.result.status = SubAgentStatus.COMPLETED.value
        agent.result.rounds_used = 5
        orch._agents['b'] = agent
        status = orch.get_status()
        assert status['b']['status'] == 'completed'
        assert status['b']['round'] == 5

    def test_last_action_from_tool_log(self):
        specs = [SubTaskSpec(id='e', role='researcher', objective='find')]
        orch = self._make_orchestrator(specs)
        agent = _make_agent(spec=specs[0])
        agent.result.status = SubAgentStatus.RUNNING.value
        agent.result.tool_log = ['tool_a', 'tool_b']
        orch._agents['e'] = agent
        status = orch.get_status()
        assert status['e']['last_action'] == 'tool_b'


@pytest.mark.unit
class TestMasterOrchestratorAbort:
    def test_abort_sets_flag(self):
        specs = [SubTaskSpec(id='a', role='researcher', objective='find')]
        orch = MasterOrchestrator(task_id='t', conv_id='c', specs=specs)
        assert not orch._aborted
        orch.abort()
        assert orch._aborted


# ═══════════════════════════════════════════════════════════
#  Bug Regression Tests (from debug/test_swarm_bugs.py)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBugA_DependencyInjection:
    """Bug A: dependency results injected into downstream agents."""

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_dependency_context_is_injected(self, mock_spawn):
        captured_specs = []

        def fake_spawn(spec, **kwargs):
            captured_specs.append((spec.id, spec.context))
            agent = MagicMock()
            agent.run.return_value = SubAgentResult(
                final_answer=f'Result from {spec.id}',
                status=SubAgentStatus.COMPLETED.value,
            )
            return agent

        mock_spawn.side_effect = fake_spawn
        spec_a = SubTaskSpec(id='a', role='coder', objective='Do task A',
                             context='Context for A', depends_on=[])
        spec_b = SubTaskSpec(id='b', role='writer', objective='Do task B',
                             context='Context for B', depends_on=['a'])
        master = MasterOrchestrator(specs=[spec_a, spec_b], task_id='test-dep',
                                    conv_id='c1', all_tools=[])
        results = master.run()
        assert len(results) == 2
        b_contexts = [ctx for sid, ctx in captured_specs if sid == 'b']
        assert len(b_contexts) == 1
        assert 'Results from prerequisite tasks' in b_contexts[0]
        assert 'Result from a' in b_contexts[0]


@pytest.mark.unit
class TestBugB_AbortCheckNone:
    """Bug B: abort_check=None must not crash."""

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_run_with_none_abort_check(self, mock_spawn):
        agent = MagicMock()
        agent.run.return_value = SubAgentResult(
            final_answer='ok', status=SubAgentStatus.COMPLETED.value)
        mock_spawn.return_value = agent
        spec = SubTaskSpec(id='x', role='coder', objective='test')
        master = MasterOrchestrator(specs=[spec], task_id='t', conv_id='c',
                                    all_tools=[], abort_check=None)
        results = master.run()
        assert len(results) == 1

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_abort_check_callable_works(self, mock_spawn):
        agent = MagicMock()
        agent.run.return_value = SubAgentResult(
            final_answer='ok', status=SubAgentStatus.COMPLETED.value)
        mock_spawn.return_value = agent
        check_calls = []

        def my_check():
            check_calls.append(1)
            return False

        spec = SubTaskSpec(id='x', role='coder', objective='test')
        master = MasterOrchestrator(specs=[spec], task_id='t', conv_id='c',
                                    all_tools=[], abort_check=my_check)
        results = master.run()
        assert len(results) == 1
        assert len(check_calls) > 0


@pytest.mark.unit
class TestBugC_ResultsType:
    """Bug C: _results must store (SubTaskSpec, SubAgentResult) tuples."""

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_results_are_tuples(self, mock_spawn):
        agent = MagicMock()
        agent.run.return_value = SubAgentResult(
            final_answer='done', status=SubAgentStatus.COMPLETED.value)
        mock_spawn.return_value = agent
        spec = SubTaskSpec(id='t1', role='coder', objective='task')
        master = MasterOrchestrator(specs=[spec], task_id='t', conv_id='c', all_tools=[])
        results = master.run()
        assert len(results) == 1
        item = results[0]
        assert isinstance(item, tuple)
        assert isinstance(item[0], SubTaskSpec)
        assert isinstance(item[1], SubAgentResult)


# ═══════════════════════════════════════════════════════════
#  Integration + Tool Defs
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSwarmToolDefs:
    def test_tool_names_not_empty(self):
        assert len(SWARM_TOOL_NAMES) > 0

    def test_spawn_agents_in_names(self):
        assert 'spawn_agents' in SWARM_TOOL_NAMES

    def test_spawn_agents_tool_structure(self):
        assert SPAWN_AGENTS_TOOL['type'] == 'function'
        assert 'name' in SPAWN_AGENTS_TOOL['function']
        assert 'description' in SPAWN_AGENTS_TOOL['function']
        assert 'parameters' in SPAWN_AGENTS_TOOL['function']
        assert SPAWN_AGENTS_TOOL['function']['name'] == 'spawn_agents'


@pytest.mark.unit
class TestSwarmRoutes:
    def test_blueprint_exists(self):
        from routes.swarm import swarm_bp
        assert swarm_bp.name == 'swarm'
