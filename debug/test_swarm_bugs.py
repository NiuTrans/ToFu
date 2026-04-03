#!/usr/bin/env python3
"""
Swarm Bug Regression Tests
===========================
Targeted tests for 3 bugs found during code audit:

Bug A (HIGH): MasterOrchestrator.run() missing dependency context injection
Bug B (HIGH): abort_check lambda crashes when self.abort_check is None
Bug C (MEDIUM): _results type annotation was wrong (list[SubAgentResult] vs
                list[tuple[SubTaskSpec, SubAgentResult]])
"""

import sys
import os
import time
import unittest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.swarm.protocol import (
    SubTaskSpec, SubAgentStatus, AgentMessage, compress_result,
)
from lib.swarm.agent import SubAgentResult
from lib.swarm.master import (
    resolve_execution_order, MasterOrchestrator,
)


class TestBugA_DependencyInjection(unittest.TestCase):
    """Bug A: MasterOrchestrator.run() must inject dependency results
    into downstream agents' context, matching run_swarm_task() behavior."""

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_dependency_context_is_injected(self, mock_spawn):
        """When spec B depends_on spec A, B should receive A's result
        appended to its context before the agent is spawned."""

        # Track what contexts are passed to spawn_sub_agent
        captured_specs = []

        def fake_spawn(spec, **kwargs):
            # Deep-copy the spec context at call time
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

        master = MasterOrchestrator(
            specs=[spec_a, spec_b],
            task_id='test-dep',
            conv_id='c1',
            all_tools=[],
        )
        results = master.run()

        # Spec A should have been spawned first (wave 1)
        # Spec B should have been spawned second (wave 2) with A's result
        self.assertEqual(len(results), 2)

        # Find spec B's context at spawn time
        b_contexts = [ctx for sid, ctx in captured_specs if sid == 'b']
        self.assertEqual(len(b_contexts), 1, "spec b should be spawned exactly once")
        b_context = b_contexts[0]

        self.assertIn('Results from prerequisite tasks', b_context,
                      "Bug A regression: downstream agent must receive "
                      "dependency results in context")
        self.assertIn('Result from a', b_context,
                      "Bug A regression: dependency result content must appear")

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_no_injection_when_no_dependencies(self, mock_spawn):
        """Specs without depends_on should not get extra context."""

        captured_specs = []

        def fake_spawn(spec, **kwargs):
            captured_specs.append((spec.id, spec.context))
            agent = MagicMock()
            agent.run.return_value = SubAgentResult(
                final_answer='done',
                status=SubAgentStatus.COMPLETED.value,
            )
            return agent

        mock_spawn.side_effect = fake_spawn

        spec = SubTaskSpec(id='solo', role='coder', objective='Solo task',
                           context='Original context', depends_on=[])

        master = MasterOrchestrator(specs=[spec], task_id='t', conv_id='c',
                                    all_tools=[])
        master.run()

        solo_ctx = [ctx for sid, ctx in captured_specs if sid == 'solo'][0]
        self.assertNotIn('Results from prerequisite tasks', solo_ctx,
                         "Specs without dependencies should not get injected context")
        self.assertEqual(solo_ctx, 'Original context')


class TestBugB_AbortCheckNone(unittest.TestCase):
    """Bug B: MasterOrchestrator.run() must not crash when abort_check is None."""

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_run_with_none_abort_check(self, mock_spawn):
        """When abort_check=None (the default), run() should not raise TypeError."""
        agent = MagicMock()
        agent.run.return_value = SubAgentResult(
            final_answer='ok',
            status=SubAgentStatus.COMPLETED.value,
        )
        mock_spawn.return_value = agent

        spec = SubTaskSpec(id='x', role='coder', objective='test')

        master = MasterOrchestrator(
            specs=[spec], task_id='t', conv_id='c', all_tools=[],
            abort_check=None,  # Explicitly None — the default
        )

        # This must not raise TypeError: 'NoneType' object is not callable
        try:
            results = master.run()
        except TypeError as e:
            if 'NoneType' in str(e) and 'callable' in str(e):
                self.fail(f"Bug B regression: abort_check=None causes crash: {e}")
            raise

        self.assertEqual(len(results), 1)

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_abort_check_callable_works(self, mock_spawn):
        """When abort_check is a callable, it should be invoked normally."""
        agent = MagicMock()
        agent.run.return_value = SubAgentResult(
            final_answer='ok', status=SubAgentStatus.COMPLETED.value)
        mock_spawn.return_value = agent

        check_calls = []
        def my_check():
            check_calls.append(1)
            return False

        spec = SubTaskSpec(id='x', role='coder', objective='test')
        master = MasterOrchestrator(
            specs=[spec], task_id='t', conv_id='c', all_tools=[],
            abort_check=my_check,
        )
        results = master.run()
        self.assertEqual(len(results), 1)
        # The callable should have been invoked at least once
        self.assertGreater(len(check_calls), 0,
                           "abort_check callable should be invoked during run()")


class TestBugC_ResultsTypeAnnotation(unittest.TestCase):
    """Bug C: _results must store (SubTaskSpec, SubAgentResult) tuples."""

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_results_are_tuples(self, mock_spawn):
        """Each element in _results must be a (SubTaskSpec, SubAgentResult) tuple."""
        agent = MagicMock()
        agent.run.return_value = SubAgentResult(
            final_answer='done', status=SubAgentStatus.COMPLETED.value)
        mock_spawn.return_value = agent

        spec = SubTaskSpec(id='t1', role='coder', objective='task')
        master = MasterOrchestrator(specs=[spec], task_id='t', conv_id='c',
                                    all_tools=[])
        results = master.run()

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertIsInstance(item, tuple,
                              "Bug C: _results items must be tuples")
        self.assertEqual(len(item), 2)
        self.assertIsInstance(item[0], SubTaskSpec,
                              "Bug C: first element must be SubTaskSpec")
        self.assertIsInstance(item[1], SubAgentResult,
                              "Bug C: second element must be SubAgentResult")

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_results_by_id_populated(self, mock_spawn):
        """After run(), all specs should have entries in results."""
        agent = MagicMock()
        agent.run.return_value = SubAgentResult(
            final_answer='ok', status=SubAgentStatus.COMPLETED.value)
        mock_spawn.return_value = agent

        specs = [SubTaskSpec(id=f's{i}', role='coder', objective=f'task {i}')
                 for i in range(3)]
        master = MasterOrchestrator(specs=specs, task_id='t', conv_id='c',
                                    all_tools=[])
        results = master.run()
        self.assertEqual(len(results), 3)
        result_ids = {spec.id for spec, _ in results}
        self.assertEqual(result_ids, {'s0', 's1', 's2'})


class TestResolveExecutionOrder(unittest.TestCase):
    """Additional edge-case tests for resolve_execution_order."""

    def test_depends_on_none(self):
        """specs with depends_on=None should not crash resolve_execution_order."""
        spec = SubTaskSpec(id='a', role='coder', objective='test')
        spec.depends_on = None  # Simulate bad input
        waves = resolve_execution_order([spec])
        self.assertEqual(len(waves), 1)
        self.assertEqual(waves[0][0].id, 'a')

    def test_empty_specs(self):
        """Empty spec list should return empty waves."""
        self.assertEqual(resolve_execution_order([]), [])

    def test_diamond_dependency(self):
        """Diamond: A -> B,C -> D. Should produce 3 waves."""
        a = SubTaskSpec(id='a', objective='root')
        b = SubTaskSpec(id='b', objective='left', depends_on=['a'])
        c = SubTaskSpec(id='c', objective='right', depends_on=['a'])
        d = SubTaskSpec(id='d', objective='merge', depends_on=['b', 'c'])
        waves = resolve_execution_order([a, b, c, d])
        self.assertEqual(len(waves), 3)
        self.assertEqual(waves[0][0].id, 'a')
        wave2_ids = {s.id for s in waves[1]}
        self.assertEqual(wave2_ids, {'b', 'c'})
        self.assertEqual(waves[2][0].id, 'd')


class TestCompressResultEdgeCases(unittest.TestCase):
    """Edge cases for compress_result utility."""

    def test_empty_string(self):
        result = compress_result('')
        self.assertIsInstance(result, str)

    def test_none_input(self):
        """compress_result should handle None gracefully."""
        try:
            result = compress_result(None)
            # Should either return a string or raise TypeError
            self.assertIsInstance(result, str)
        except (TypeError, AttributeError):
            pass  # Acceptable — the caller should guard against None

    def test_long_input_truncated(self):
        long_text = 'x' * 50000
        result = compress_result(long_text, max_chars=1000)
        self.assertLessEqual(len(result), 1500,  # Allow some margin
                             "compress_result should truncate long inputs")


if __name__ == '__main__':
    # Run with verbose output
    unittest.main(verbosity=2)
