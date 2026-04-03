#!/usr/bin/env python3
"""
Swarm Feature — Comprehensive Unit Tests
==========================================

Tests the agent swarm system for correctness, including regressions
for bugs discovered during code review:

BUG-1 (FIXED): MasterOrchestrator.get_status() used wrong attribute
    names (agent._status, agent._round, agent._last_action) instead
    of the real ones (agent.result.status, agent.result.rounds_used,
    agent.result.tool_log). Now verified with regression tests.

BUG-2 (FIXED): AgentMessage had to_dict() but no from_dict(),
    breaking symmetric serialization. Now added.

Run:  python debug/test_swarm.py
  or: python -m pytest debug/test_swarm.py -v
"""

import sys, os, unittest
from unittest.mock import MagicMock

# Ensure project root on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ═════════════════════════════════════════════════════════════
#  Module imports
# ═════════════════════════════════════════════════════════════
from lib.swarm.protocol import (
    SubTaskSpec, AgentMessage, SubAgentStatus,
    MAX_COMPRESSED_RESULT_CHARS, compress_result,
    format_sub_results_for_master,
)
from lib.swarm.agent import SubAgent, SubAgentResult
from lib.swarm.registry import (
    AGENT_ROLES, get_role, filter_tools_for_role, get_model_for_role,
)
from lib.swarm.master import (
    resolve_execution_order,
    MasterOrchestrator,
)
from lib.swarm.integration import should_auto_swarm
from lib.swarm.tools import SWARM_TOOL_DEFS


# ═════════════════════════════════════════════════════════════
#  1. Protocol Tests
# ═════════════════════════════════════════════════════════════
class TestSubTaskSpec(unittest.TestCase):
    """Test SubTaskSpec dataclass."""

    def test_create_basic(self):
        spec = SubTaskSpec(id='a1', role='researcher', objective='find X')
        self.assertEqual(spec.id, 'a1')
        self.assertEqual(spec.role, 'researcher')
        self.assertEqual(spec.objective, 'find X')
        self.assertEqual(spec.depends_on, [])

    def test_with_dependencies(self):
        spec = SubTaskSpec(id='b', role='coder', objective='code Y',
                           depends_on=['a1'])
        self.assertEqual(spec.depends_on, ['a1'])

    def test_to_dict_from_dict_roundtrip(self):
        spec = SubTaskSpec(id='c', role='writer', objective='write Z',
                           depends_on=['a1', 'b'])
        d = spec.to_dict()
        spec2 = SubTaskSpec.from_dict(d)
        self.assertEqual(spec2.id, 'c')
        self.assertEqual(spec2.depends_on, ['a1', 'b'])

    def test_from_dict_ignores_unknown(self):
        d = {'id': 'x', 'role': 'coder', 'objective': 'hi',
             'random_extra_field': True}
        spec = SubTaskSpec.from_dict(d)
        self.assertEqual(spec.id, 'x')


class TestAgentMessage(unittest.TestCase):
    """Test AgentMessage dataclass — including BUG-2 regression."""

    def test_create(self):
        msg = AgentMessage(sender='a1', receiver='master', content='done')
        self.assertEqual(msg.sender, 'a1')
        self.assertEqual(msg.receiver, 'master')
        self.assertEqual(msg.content, 'done')

    def test_to_dict(self):
        msg = AgentMessage(sender='a1', receiver='master', content='done')
        d = msg.to_dict()
        self.assertEqual(d['sender'], 'a1')
        self.assertIn('timestamp', d)

    def test_from_dict_regression_bug2(self):
        """BUG-2 regression: from_dict() must exist and work."""
        d = {'sender': 'master', 'receiver': 'a1',
             'content': 'go', 'msg_type': 'instruction'}
        msg = AgentMessage.from_dict(d)
        self.assertEqual(msg.sender, 'master')
        self.assertEqual(msg.msg_type, 'instruction')

    def test_roundtrip(self):
        """to_dict() → from_dict() should preserve all fields."""
        original = AgentMessage(sender='x', receiver='y', content='hello',
                                msg_type='result')
        restored = AgentMessage.from_dict(original.to_dict())
        self.assertEqual(restored.sender, 'x')
        self.assertEqual(restored.receiver, 'y')
        self.assertEqual(restored.content, 'hello')
        self.assertEqual(restored.msg_type, 'result')

    def test_from_dict_ignores_unknown_keys(self):
        d = {'sender': 'a', 'receiver': 'b', 'content': 'c',
             'totally_fake_field': 999}
        msg = AgentMessage.from_dict(d)
        self.assertEqual(msg.sender, 'a')
        self.assertFalse(hasattr(msg, 'totally_fake_field'))


class TestSubAgentStatus(unittest.TestCase):

    def test_values(self):
        self.assertEqual(SubAgentStatus.PENDING.value, 'pending')
        self.assertEqual(SubAgentStatus.RUNNING.value, 'running')
        self.assertEqual(SubAgentStatus.COMPLETED.value, 'completed')
        self.assertEqual(SubAgentStatus.FAILED.value, 'failed')


class TestCompressResult(unittest.TestCase):

    def test_short_passthrough(self):
        self.assertEqual(compress_result('hello', 'test'), 'hello')

    def test_empty_input(self):
        self.assertEqual(compress_result('', 'test'), '(no output)')

    def test_long_input_compressed(self):
        big = 'X' * (MAX_COMPRESSED_RESULT_CHARS + 1000)
        r = compress_result(big, 'test')
        self.assertLessEqual(len(r), MAX_COMPRESSED_RESULT_CHARS + 100)


class TestFormatResultsForMaster(unittest.TestCase):

    def test_completed_result(self):
        spec = SubTaskSpec(id='spec-a', role='researcher', objective='find X')
        r = SubAgentResult()
        r.status = 'completed'
        r.final_answer = 'The answer is 42.'
        r.rounds_used = 3
        r.total_tokens = 1000
        results = [(spec, r)]
        txt = format_sub_results_for_master(results)
        # Format uses spec.role + spec.objective, not spec.id
        self.assertIn('researcher', txt)
        self.assertIn('find X', txt)
        self.assertIn('42', txt)
        self.assertIn('completed', txt)

    def test_failed_result(self):
        spec = SubTaskSpec(id='spec-b', role='coder', objective='code Y')
        r = SubAgentResult()
        r.status = 'failed'
        r.error_message = 'something broke'
        results = [(spec, r)]
        txt = format_sub_results_for_master(results)
        self.assertIn('failed', txt)
        self.assertIn('something broke', txt)


# ═════════════════════════════════════════════════════════════
#  2. Registry Tests
# ═════════════════════════════════════════════════════════════
class TestRegistry(unittest.TestCase):

    def test_roles_not_empty(self):
        self.assertGreater(len(AGENT_ROLES), 0)

    def test_general_role_exists(self):
        role = get_role('general')
        self.assertIsInstance(role, dict)
        self.assertIn('system_prompt_suffix', role)

    def test_unknown_role_falls_back(self):
        role = get_role('nonexistent_role_xyz')
        general = get_role('general')
        self.assertEqual(role, general)

    def test_all_roles_have_required_keys(self):
        for name, role in AGENT_ROLES.items():
            with self.subTest(role=name):
                self.assertIn('system_prompt_suffix', role)
                self.assertIn('tool_categories', role)
                self.assertIsInstance(role['tool_categories'], list)

    def test_filter_tools_returns_list(self):
        tools = [{'name': 'a'}, {'name': 'b'}, {'name': 'c'}]
        filtered = filter_tools_for_role(tools, 'researcher')
        self.assertIsInstance(filtered, list)
        self.assertGreater(len(filtered), 0)

    def test_get_model_for_role(self):
        avail = {'strong': 'gpt-4', 'fast': 'gpt-3.5'}
        m = get_model_for_role('researcher', avail)
        self.assertIn(m, ['gpt-4', 'gpt-3.5'])

    def test_get_model_for_unknown_role(self):
        avail = {'strong': 'claude-4', 'fast': 'haiku'}
        m = get_model_for_role('totally_fake', avail)
        self.assertIsNotNone(m)


# ═════════════════════════════════════════════════════════════
#  3. SubAgent Tests
# ═════════════════════════════════════════════════════════════
class TestSubAgentResult(unittest.TestCase):

    def test_defaults(self):
        r = SubAgentResult()
        self.assertEqual(r.status, 'pending')
        self.assertEqual(r.final_answer, '')
        self.assertEqual(r.error_message, '')
        self.assertEqual(r.rounds_used, 0)
        self.assertEqual(r.total_tokens, 0)
        self.assertEqual(r.cost_usd, 0.0)
        self.assertEqual(r.artifacts, [])


class TestSubAgentInit(unittest.TestCase):
    """Test SubAgent construction (uses all_tools= kwarg)."""

    def _make_agent(self, **overrides):
        defaults = dict(
            spec=SubTaskSpec(id='test-1', role='researcher',
                             objective='do research'),
            parent_task={'conv_id': 'c1', 'task_id': 't1'},
            all_tools=[],
            system_prompt_base='You are helpful.',
            model='test-model',
        )
        defaults.update(overrides)
        return SubAgent(**defaults)

    def test_basic_creation(self):
        agent = self._make_agent()
        self.assertEqual(agent.spec.id, 'test-1')
        self.assertEqual(agent.model, 'test-model')
        self.assertEqual(agent.result.status, 'pending')
        self.assertIsInstance(agent.agent_id, str)
        self.assertTrue(agent.agent_id.startswith('sub-'))

    def test_max_rounds_from_role(self):
        agent = self._make_agent()
        self.assertGreater(agent.max_rounds, 0)

    def test_result_is_subagentresult(self):
        agent = self._make_agent(
            spec=SubTaskSpec(id='y', role='coder', objective='code'))
        self.assertIsInstance(agent.result, SubAgentResult)


# ═════════════════════════════════════════════════════════════
#  4. Master Orchestrator Tests
# ═════════════════════════════════════════════════════════════
class TestResolveExecutionOrder(unittest.TestCase):

    def test_no_deps(self):
        specs = [
            SubTaskSpec(id='a', role='researcher', objective='x'),
            SubTaskSpec(id='b', role='coder', objective='y'),
        ]
        order = resolve_execution_order(specs)
        self.assertIsInstance(order, list)
        self.assertGreater(len(order), 0)

    def test_chain_dependency(self):
        specs = [
            SubTaskSpec(id='a', role='researcher', objective='find'),
            SubTaskSpec(id='b', role='coder', objective='code',
                        depends_on=['a']),
            SubTaskSpec(id='c', role='writer', objective='write',
                        depends_on=['b']),
        ]
        order = resolve_execution_order(specs)
        # Waves contain SubTaskSpec objects, extract IDs
        flat = [s.id for wave in order for s in wave]
        self.assertLess(flat.index('a'), flat.index('b'))
        self.assertLess(flat.index('b'), flat.index('c'))

    def test_parallel_deps(self):
        specs = [
            SubTaskSpec(id='a', role='researcher', objective='1'),
            SubTaskSpec(id='b', role='researcher', objective='2'),
            SubTaskSpec(id='c', role='writer', objective='merge',
                        depends_on=['a', 'b']),
        ]
        order = resolve_execution_order(specs)
        # Waves contain SubTaskSpec objects, extract IDs
        flat = [s.id for wave in order for s in wave]
        self.assertLess(flat.index('a'), flat.index('c'))
        self.assertLess(flat.index('b'), flat.index('c'))


class TestMasterOrchestratorGetStatus(unittest.TestCase):
    """
    ★ BUG-1 Regression Tests ★
    Verifies that get_status() reads the correct attributes from SubAgent
    after the fix (agent.result.status, agent.result.rounds_used, etc.).
    """

    def _make_orchestrator(self, specs):
        return MasterOrchestrator(
            task_id='t1', conv_id='c1', specs=specs, model='test-model',
        )

    def _make_agent(self, spec, **overrides):
        defaults = dict(
            spec=spec,
            parent_task={'conv_id': 'c1', 'task_id': 't1'},
            all_tools=[],
            model='test-model',
        )
        defaults.update(overrides)
        return SubAgent(**defaults)

    def test_pending_status_before_run(self):
        specs = [SubTaskSpec(id='a', role='researcher', objective='find')]
        orch = self._make_orchestrator(specs)
        status = orch.get_status()
        self.assertEqual(status['a']['status'], 'pending')

    def test_regression_bug1_live_agent_status(self):
        """After fix, get_status() must report the real status, not 'unknown'."""
        specs = [SubTaskSpec(id='a', role='researcher', objective='find')]
        orch = self._make_orchestrator(specs)

        agent = self._make_agent(specs[0])
        agent.result.status = SubAgentStatus.RUNNING.value
        agent.result.rounds_used = 3
        orch._agents[specs[0].id] = agent

        status = orch.get_status()
        self.assertEqual(status['a']['status'], 'running',
                         "BUG-1 regression: status must reflect agent.result.status")
        self.assertEqual(status['a']['round'], 3,
                         "BUG-1 regression: round must reflect agent.result.rounds_used")

    def test_regression_bug1_completed_agent(self):
        specs = [SubTaskSpec(id='b', role='coder', objective='code')]
        orch = self._make_orchestrator(specs)

        agent = self._make_agent(specs[0])
        agent.result.status = SubAgentStatus.COMPLETED.value
        agent.result.rounds_used = 5
        orch._agents['b'] = agent

        status = orch.get_status()
        self.assertEqual(status['b']['status'], 'completed')
        self.assertEqual(status['b']['round'], 5)

    def test_regression_bug1_failed_agent(self):
        specs = [SubTaskSpec(id='c', role='writer', objective='write')]
        orch = self._make_orchestrator(specs)

        agent = self._make_agent(specs[0])
        agent.result.status = SubAgentStatus.FAILED.value
        agent.result.rounds_used = 1
        orch._agents['c'] = agent

        status = orch.get_status()
        self.assertEqual(status['c']['status'], 'failed')
        self.assertEqual(status['c']['round'], 1)

    def test_max_rounds_is_correct(self):
        specs = [SubTaskSpec(id='d', role='researcher', objective='find')]
        orch = self._make_orchestrator(specs)

        agent = self._make_agent(specs[0])
        orch._agents['d'] = agent

        status = orch.get_status()
        self.assertEqual(status['d']['max_rounds'], agent.max_rounds)

    def test_last_action_from_tool_log(self):
        """After fix, last_action comes from agent.result.tool_log."""
        specs = [SubTaskSpec(id='e', role='researcher', objective='find')]
        orch = self._make_orchestrator(specs)

        agent = self._make_agent(specs[0])
        agent.result.status = SubAgentStatus.RUNNING.value
        agent.result.tool_log = ['tool_a', 'tool_b']
        orch._agents['e'] = agent

        status = orch.get_status()
        self.assertEqual(status['e']['last_action'], 'tool_b',
                         "last_action should come from tool_log[-1]")


class TestMasterOrchestratorAbort(unittest.TestCase):

    def test_abort_sets_flag(self):
        specs = [SubTaskSpec(id='a', role='researcher', objective='find')]
        orch = MasterOrchestrator(task_id='t', conv_id='c', specs=specs)
        self.assertFalse(orch._aborted)
        orch.abort()
        self.assertTrue(orch._aborted)


# ═════════════════════════════════════════════════════════════
#  5. Integration Tests
# ═════════════════════════════════════════════════════════════
class TestShouldAutoSwarm(unittest.TestCase):

    def test_short_message_no_swarm(self):
        self.assertFalse(should_auto_swarm("hi"))

    def test_explicit_keywords(self):
        long_msg = (
            "I need you to simultaneously research topic A, "
            "write code for B, and create documentation for C."
        )
        result = should_auto_swarm(long_msg)
        self.assertIsInstance(result, bool)


# ═════════════════════════════════════════════════════════════
#  6. Swarm Tool Definitions
# ═════════════════════════════════════════════════════════════
class TestSwarmToolDefs(unittest.TestCase):

    def test_defs_not_empty(self):
        self.assertGreater(len(SWARM_TOOL_DEFS), 0)

    def test_spawn_agents_tool_exists(self):
        names = [t['function']['name'] for t in SWARM_TOOL_DEFS]
        self.assertIn('spawn_agents', names)

    def test_tool_structure(self):
        for tool in SWARM_TOOL_DEFS:
            with self.subTest(tool=tool.get('function', {}).get('name', '?')):
                self.assertEqual(tool['type'], 'function')
                self.assertIn('name', tool['function'])
                self.assertIn('description', tool['function'])
                self.assertIn('parameters', tool['function'])


# ═════════════════════════════════════════════════════════════
#  7. Routes Tests
# ═════════════════════════════════════════════════════════════
class TestRoutes(unittest.TestCase):

    def test_blueprint_exists(self):
        from routes.swarm import swarm_bp
        self.assertEqual(swarm_bp.name, 'swarm')

    def test_blueprint_importable(self):
        from routes.swarm import swarm_bp
        self.assertIsNotNone(swarm_bp)


# ═════════════════════════════════════════════════════════════
#  Runner
# ═════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print("=" * 60)
    print("  Swarm Feature — Comprehensive Test Suite")
    print("=" * 60)
    print()
    unittest.main(verbosity=2)
