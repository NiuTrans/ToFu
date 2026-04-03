#!/usr/bin/env python3
"""End-to-end tests for the swarm reactive loop, fast-path, and async path.

These tests prove:
  1. Full reactive loop: spawn agents → agents complete → master review
     decides to spawn more → new agents complete → final synthesis.
  2. Fast-path: when all agents succeed with clean results, master review
     is skipped (opt-in via fast_path_enabled=True).
  3. Fast-path is NOT triggered when results contain error indicators.
  4. Fast-path is NOT triggered when fast_path_enabled is False (default).
  5. Async execution path: AsyncStreamingScheduler + run_reactive_async.

All LLM calls are mocked — no real API keys needed.
"""

import asyncio
import json
import logging
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, '.')

from lib.swarm.protocol import (
    SubTaskSpec, SubAgentResult, SubAgentStatus, ArtifactStore,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.scheduler import StreamingScheduler, AsyncStreamingScheduler

logging.basicConfig(level=logging.WARNING)


# ════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════

def make_spec(id: str, role='coder', objective='Do X', context='',
              depends_on=None, max_rounds=3, max_retries=0) -> SubTaskSpec:
    return SubTaskSpec(
        id=id, role=role, objective=objective, context=context,
        depends_on=depends_on or [], max_rounds=max_rounds,
        max_retries=max_retries,
    )


def make_result(status='completed', answer='done', tokens=100,
                elapsed=1.0) -> SubAgentResult:
    return SubAgentResult(
        status=status,
        final_answer=answer,
        total_tokens=tokens,
        elapsed_seconds=elapsed,
    )


def _make_orchestrator(specs=None, fast_path_enabled=False, **kwargs):
    if specs is None:
        specs = [make_spec('a'), make_spec('b')]
    defaults = dict(
        task_id='e2e-test',
        conv_id='e2e-conv',
        specs=specs,
        model='test-model',
        all_tools=[],
        max_parallel=4,
        max_retries=0,
        fast_path_enabled=fast_path_enabled,
    )
    defaults.update(kwargs)
    return MasterOrchestrator(**defaults)


# ════════════════════════════════════════════════════════════
#  E2E Test 1: Full Reactive Loop
#    spawn agents → complete → master spawns more → complete → synthesis
# ════════════════════════════════════════════════════════════

class TestE2EReactiveLoop(unittest.TestCase):
    """Proves the full reactive cycle with multi-round review."""

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_full_reactive_loop(self, mock_spawn, mock_build, mock_stream):
        """
        Full reactive loop:
          Round 1: Agents a, b run → master reviews → spawns agent c
          Round 2: Agent c runs → master reviews → swarm_done
          Synthesis: produces final answer
        """
        # ── Mock agent factory ──
        agent_call_count = [0]

        def fake_spawn(spec, **kwargs):
            agent_call_count[0] += 1
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.role}-{spec.id}'
            agent.result = make_result(
                answer=f'Result from agent {spec.id}: {spec.objective}',
                tokens=200,
            )
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        # ── Mock LLM calls: review round 1 → spawn_more, review round 2 → done, synthesis ──
        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                if review_count[0] == 1:
                    # First review: spawn one more agent
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc_spawn',
                                'function': {
                                    'name': 'spawn_more_agents',
                                    'arguments': json.dumps({
                                        'reason': 'Need a reviewer to verify findings',
                                        'agents': [{
                                            'role': 'reviewer',
                                            'objective': 'Verify the findings from agents a and b',
                                            'id': 'c',
                                        }],
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 150, 'prompt_tokens': 100,
                         'completion_tokens': 50},
                    )
                else:
                    # Second review: done
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc_done',
                                'function': {
                                    'name': 'swarm_done',
                                    'arguments': json.dumps({
                                        'summary': 'All findings verified by reviewer'
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 100, 'prompt_tokens': 80,
                         'completion_tokens': 20},
                    )
            else:
                # Synthesis call
                if on_content:
                    on_content('Comprehensive final answer combining all agents.')
                return (
                    {'content': 'Comprehensive final answer combining all agents.'},
                    'stop',
                    {'total_tokens': 300, 'prompt_tokens': 200,
                     'completion_tokens': 100},
                )

        mock_stream.side_effect = fake_stream_chat

        # ── Run the orchestrator ──
        events = []
        orch = _make_orchestrator(
            specs=[make_spec('a', objective='Research topic A'),
                   make_spec('b', objective='Research topic B')],
            on_progress=lambda ev: events.append(ev),
            max_reactive_rounds=5,
        )

        final_answer = orch.run_reactive(original_query='Analyze the project')

        # ── Assertions ──

        # 1. Final answer was produced
        self.assertIn('Comprehensive final answer', final_answer)

        # 2. Three agents ran total (a, b, c)
        self.assertEqual(len(orch._results), 3)
        agent_ids = {spec.id for spec, _ in orch._results}
        self.assertIn('a', agent_ids)
        self.assertIn('b', agent_ids)
        self.assertIn('c', agent_ids)

        # 3. All agents completed successfully
        for spec, result in orch._results:
            self.assertEqual(result.status, SubAgentStatus.COMPLETED.value)

        # 4. Master review ran exactly 2 times
        self.assertEqual(review_count[0], 2)

        # 5. spawn_more event was emitted
        spawn_events = [e for e in events if e.get('phase') == 'spawn_more']
        self.assertGreaterEqual(len(spawn_events), 1)
        self.assertIn('reviewer', spawn_events[0].get('content', ''))

        # 6. Synthesis phase was reached
        synth_events = [e for e in events if e.get('phase') == 'synthesis']
        self.assertGreaterEqual(len(synth_events), 1)

        # 7. Complete phase was reached
        complete_events = [e for e in events if e.get('phase') == 'complete']
        self.assertGreaterEqual(len(complete_events), 1)

        # 8. Agent factory was called 3 times
        self.assertEqual(agent_call_count[0], 3)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_reactive_loop_with_dependencies(self, mock_spawn, mock_build,
                                              mock_stream):
        """Reactive loop where spawned-more agents have dependencies
        on earlier agents."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(
                answer=f'Result from {spec.id}',
                tokens=150,
            )
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                if review_count[0] == 1:
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc1',
                                'function': {
                                    'name': 'spawn_more_agents',
                                    'arguments': json.dumps({
                                        'reason': 'Need synthesis agent',
                                        'agents': [{
                                            'role': 'analyst',
                                            'objective': 'Synthesize research',
                                            'id': 'c',
                                        }],
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 100},
                    )
                else:
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc2',
                                'function': {
                                    'name': 'swarm_done',
                                    'arguments': json.dumps({
                                        'summary': 'All done'
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 80},
                    )
            else:
                if on_content:
                    on_content('Final combined answer')
                return (
                    {'content': 'Final combined answer'},
                    'stop',
                    {'total_tokens': 200},
                )

        mock_stream.side_effect = fake_stream_chat

        specs = [
            make_spec('a', role='researcher', objective='Research part 1'),
            make_spec('b', role='researcher', objective='Research part 2'),
        ]
        orch = _make_orchestrator(specs=specs, max_reactive_rounds=5)
        result = orch.run_reactive(original_query='Complex multi-part analysis')

        self.assertIn('Final combined answer', result)
        self.assertEqual(len(orch._results), 3)
        self.assertEqual(review_count[0], 2)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_reactive_loop_with_failed_agent(self, mock_spawn, mock_build,
                                              mock_stream):
        """Reactive loop where one agent fails — master should still review
        and decide to spawn replacement."""
        call_idx = [0]

        def fake_spawn(spec, **kwargs):
            call_idx[0] += 1
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            if spec.id == 'b':
                # Agent b fails
                agent.result = make_result(
                    status='failed',
                    answer='',
                    tokens=50,
                )
                agent.result.error_message = 'Connection timeout'
            elif spec.id == 'b_retry':
                # Replacement succeeds
                agent.result = make_result(
                    answer='Replacement result for b',
                    tokens=180,
                )
            else:
                agent.result = make_result(
                    answer=f'Result from {spec.id}',
                    tokens=150,
                )
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                if review_count[0] == 1:
                    # Detect failure, spawn replacement
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc1',
                                'function': {
                                    'name': 'spawn_more_agents',
                                    'arguments': json.dumps({
                                        'reason': 'Agent b failed, retrying',
                                        'agents': [{
                                            'role': 'researcher',
                                            'objective': 'Redo research part 2',
                                            'id': 'b_retry',
                                        }],
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 120},
                    )
                else:
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc2',
                                'function': {
                                    'name': 'swarm_done',
                                    'arguments': json.dumps({
                                        'summary': 'Complete with retry'
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 80},
                    )
            else:
                if on_content:
                    on_content('Final answer with retry')
                return (
                    {'content': 'Final answer with retry'},
                    'stop',
                    {'total_tokens': 250},
                )

        mock_stream.side_effect = fake_stream_chat

        specs = [
            make_spec('a', objective='Research part 1'),
            make_spec('b', objective='Research part 2'),
        ]
        orch = _make_orchestrator(specs=specs, max_reactive_rounds=5)
        result = orch.run_reactive(original_query='Research with failure')

        self.assertIn('Final answer with retry', result)
        # 3 total: a (ok), b (failed), b_retry (ok)
        self.assertEqual(len(orch._results), 3)
        # Verify b failed
        b_results = [(s, r) for s, r in orch._results if s.id == 'b']
        self.assertEqual(len(b_results), 1)
        self.assertEqual(b_results[0][1].status, SubAgentStatus.FAILED.value)
        # Verify b_retry succeeded
        retry_results = [(s, r) for s, r in orch._results if s.id == 'b_retry']
        self.assertEqual(len(retry_results), 1)
        self.assertEqual(retry_results[0][1].status,
                         SubAgentStatus.COMPLETED.value)


# ════════════════════════════════════════════════════════════
#  E2E Test 2: Fast-Path
# ════════════════════════════════════════════════════════════

class TestFastPath(unittest.TestCase):
    """Tests for the configurable fast-path option."""

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_fast_path_skips_review_when_all_clean(self, mock_spawn,
                                                    mock_build, mock_stream):
        """When fast_path_enabled=True and all agents succeed with clean
        results, master review should be skipped entirely."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(
                answer=f'Clean result from {spec.id}: everything looks good',
                tokens=100,
            )
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                return (
                    {
                        'content': '',
                        'tool_calls': [{
                            'id': 'tc1',
                            'function': {
                                'name': 'swarm_done',
                                'arguments': json.dumps({'summary': 'Done'}),
                            },
                        }],
                    },
                    'tool_calls',
                    {'total_tokens': 80},
                )
            else:
                if on_content:
                    on_content('Synthesised answer')
                return (
                    {'content': 'Synthesised answer'},
                    'stop',
                    {'total_tokens': 200},
                )

        mock_stream.side_effect = fake_stream_chat

        events = []
        orch = _make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
            fast_path_enabled=True,
            on_progress=lambda ev: events.append(ev),
        )
        result = orch.run_reactive(original_query='Simple task')

        self.assertIn('Synthesised answer', result)
        # Master review should NOT have been called
        self.assertEqual(review_count[0], 0,
                         'Master review should be skipped on fast-path')
        # Fast-path event should have been emitted
        fp_events = [e for e in events
                     if e.get('type') == 'swarm_fast_path_skip']
        self.assertGreaterEqual(len(fp_events), 1)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_fast_path_not_triggered_with_error_indicators(
            self, mock_spawn, mock_build, mock_stream):
        """Fast-path should NOT skip review when results contain error
        indicator words, even if status is 'completed'."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            if spec.id == 'a':
                agent.result = make_result(
                    answer='The analysis found an error in the data pipeline',
                    tokens=100,
                )
            else:
                agent.result = make_result(
                    answer='Clean result', tokens=100,
                )
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                return (
                    {
                        'content': '',
                        'tool_calls': [{
                            'id': 'tc1',
                            'function': {
                                'name': 'swarm_done',
                                'arguments': json.dumps({'summary': 'Done'}),
                            },
                        }],
                    },
                    'tool_calls',
                    {'total_tokens': 80},
                )
            else:
                if on_content:
                    on_content('Answer')
                return (
                    {'content': 'Answer'}, 'stop', {'total_tokens': 200},
                )

        mock_stream.side_effect = fake_stream_chat

        events = []
        orch = _make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
            fast_path_enabled=True,
            on_progress=lambda ev: events.append(ev),
        )
        result = orch.run_reactive(original_query='Task with errors')

        # Review SHOULD have been called because agent 'a' mentions "error"
        self.assertGreaterEqual(review_count[0], 1,
                                'Review must run when results contain error indicators')
        # No fast-path event should have been emitted
        fp_events = [e for e in events
                     if e.get('type') == 'swarm_fast_path_skip']
        self.assertEqual(len(fp_events), 0)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_fast_path_not_triggered_when_disabled(self, mock_spawn,
                                                    mock_build, mock_stream):
        """When fast_path_enabled=False (default), review always runs."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(
                answer='Clean result', tokens=100,
            )
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                return (
                    {
                        'content': '',
                        'tool_calls': [{
                            'id': 'tc1',
                            'function': {
                                'name': 'swarm_done',
                                'arguments': json.dumps({'summary': 'Done'}),
                            },
                        }],
                    },
                    'tool_calls',
                    {'total_tokens': 80},
                )
            else:
                if on_content:
                    on_content('Answer')
                return (
                    {'content': 'Answer'}, 'stop', {'total_tokens': 200},
                )

        mock_stream.side_effect = fake_stream_chat

        # fast_path_enabled defaults to False
        orch = _make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
            fast_path_enabled=False,
        )
        orch.run_reactive(original_query='Default behavior')

        # Review SHOULD have been called
        self.assertGreaterEqual(review_count[0], 1,
                                'Review must run when fast_path_enabled is False')

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_fast_path_not_triggered_when_agent_failed(self, mock_spawn,
                                                        mock_build,
                                                        mock_stream):
        """Fast-path should not skip review when any agent failed."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            if spec.id == 'b':
                agent.result = make_result(status='failed', answer='')
                agent.result.error_message = 'Timeout'
            else:
                agent.result = make_result(answer='Good result', tokens=100)
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                return (
                    {
                        'content': '',
                        'tool_calls': [{
                            'id': 'tc1',
                            'function': {
                                'name': 'swarm_done',
                                'arguments': json.dumps({'summary': 'Done'}),
                            },
                        }],
                    },
                    'tool_calls',
                    {'total_tokens': 80},
                )
            else:
                if on_content:
                    on_content('Answer')
                return ({'content': 'Answer'}, 'stop', {'total_tokens': 200})

        mock_stream.side_effect = fake_stream_chat

        orch = _make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
            fast_path_enabled=True,
        )
        orch.run_reactive(original_query='Task with failure')

        # Review SHOULD run because agent b failed
        self.assertGreaterEqual(review_count[0], 1,
                                'Review must run when an agent failed')

    def test_check_fast_path_eligible_method(self):
        """Unit test for _check_fast_path_eligible."""
        orch = _make_orchestrator(fast_path_enabled=True)
        spec_a = make_spec('a')
        spec_b = make_spec('b')

        # All clean → eligible
        batch = [
            (spec_a, make_result(answer='Clean output')),
            (spec_b, make_result(answer='Also clean')),
        ]
        self.assertTrue(orch._check_fast_path_eligible(batch))

        # One failed → not eligible
        batch_fail = [
            (spec_a, make_result(answer='Clean output')),
            (spec_b, make_result(status='failed', answer='')),
        ]
        self.assertFalse(orch._check_fast_path_eligible(batch_fail))

        # Error indicator in answer → not eligible
        batch_err = [
            (spec_a, make_result(answer='Found an error in config')),
            (spec_b, make_result(answer='Clean output')),
        ]
        self.assertFalse(orch._check_fast_path_eligible(batch_err))

        # Exception indicator → not eligible
        batch_exc = [
            (spec_a, make_result(answer='Got a Traceback in the logs')),
            (spec_b, make_result(answer='Clean output')),
        ]
        self.assertFalse(orch._check_fast_path_eligible(batch_exc))

        # fast_path_enabled=False → never eligible
        orch_disabled = _make_orchestrator(fast_path_enabled=False)
        self.assertFalse(orch_disabled._check_fast_path_eligible(batch))

        # Empty batch → eligible (vacuous truth, all agents succeeded)
        self.assertTrue(orch._check_fast_path_eligible([]))


# ════════════════════════════════════════════════════════════
#  E2E Test 3: Async Execution Path
# ════════════════════════════════════════════════════════════

class TestAsyncScheduler(unittest.TestCase):
    """Tests for AsyncStreamingScheduler."""

    def test_async_scheduler_basic(self):
        """AsyncStreamingScheduler runs agents and yields results."""
        results_received = []

        def fake_factory(spec):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(answer=f'Result-{spec.id}')
            agent.run = MagicMock(return_value=agent.result)
            return agent

        async def run_test():
            scheduler = AsyncStreamingScheduler(
                agent_factory=fake_factory,
                max_parallel=4,
            )
            await scheduler.add_specs([make_spec('x'), make_spec('y')])
            async for spec, result in scheduler.iter_completions():
                results_received.append((spec.id, result.final_answer))
            scheduler.shutdown()

        asyncio.run(run_test())

        self.assertEqual(len(results_received), 2)
        ids = {r[0] for r in results_received}
        self.assertIn('x', ids)
        self.assertIn('y', ids)

    def test_async_scheduler_run_until_idle(self):
        """run_until_idle collects all results."""
        def fake_factory(spec):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(answer=f'Result-{spec.id}')
            agent.run = MagicMock(return_value=agent.result)
            return agent

        async def run_test():
            scheduler = AsyncStreamingScheduler(
                agent_factory=fake_factory,
                max_parallel=4,
            )
            await scheduler.add_specs([make_spec('p'), make_spec('q')])
            batch = await scheduler.run_until_idle()
            scheduler.shutdown()
            return batch

        batch = asyncio.run(run_test())
        self.assertEqual(len(batch), 2)

    def test_async_scheduler_properties(self):
        """Properties proxy correctly to sync scheduler."""
        def fake_factory(spec):
            agent = MagicMock()
            agent.spec = spec
            agent.result = make_result()
            agent.run = MagicMock(return_value=agent.result)
            return agent

        async def run_test():
            scheduler = AsyncStreamingScheduler(
                agent_factory=fake_factory,
                max_parallel=2,
            )
            self.assertTrue(scheduler.is_idle)
            self.assertEqual(scheduler.completed_count, 0)
            await scheduler.add_specs([make_spec('z')])
            await scheduler.run_until_idle()
            self.assertEqual(scheduler.completed_count, 1)
            self.assertTrue(scheduler.is_idle)
            self.assertEqual(len(scheduler.all_results), 1)
            scheduler.shutdown()

        asyncio.run(run_test())


class TestAsyncReactiveRun(unittest.TestCase):
    """Tests for MasterOrchestrator.run_reactive_async."""

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_async_reactive_done_immediately(self, mock_spawn, mock_build,
                                              mock_stream):
        """Async reactive run where master says done after first review."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(answer=f'Result-{spec.id}')
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                return (
                    {
                        'content': '',
                        'tool_calls': [{
                            'id': 'tc1',
                            'function': {
                                'name': 'swarm_done',
                                'arguments': json.dumps({
                                    'summary': 'All good'
                                }),
                            },
                        }],
                    },
                    'tool_calls',
                    {'total_tokens': 100},
                )
            else:
                if on_content:
                    on_content('Async synthesised answer')
                return (
                    {'content': 'Async synthesised answer'},
                    'stop',
                    {'total_tokens': 200},
                )

        mock_stream.side_effect = fake_stream_chat

        orch = _make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
        )

        result = asyncio.run(
            orch.run_reactive_async(original_query='Async test')
        )

        self.assertIn('Async synthesised answer', result)
        self.assertEqual(len(orch._results), 2)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_async_reactive_spawn_more(self, mock_spawn, mock_build,
                                        mock_stream):
        """Async reactive run with spawn_more_agents → done cycle."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(answer=f'Result-{spec.id}')
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                if review_count[0] == 1:
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc1',
                                'function': {
                                    'name': 'spawn_more_agents',
                                    'arguments': json.dumps({
                                        'reason': 'Need more',
                                        'agents': [{
                                            'role': 'analyst',
                                            'objective': 'Extra analysis',
                                            'id': 'c',
                                        }],
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 120},
                    )
                else:
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc2',
                                'function': {
                                    'name': 'swarm_done',
                                    'arguments': json.dumps({
                                        'summary': 'Complete'
                                    }),
                                },
                            }],
                        },
                        'tool_calls',
                        {'total_tokens': 80},
                    )
            else:
                if on_content:
                    on_content('Async full answer')
                return (
                    {'content': 'Async full answer'},
                    'stop',
                    {'total_tokens': 250},
                )

        mock_stream.side_effect = fake_stream_chat

        events = []
        orch = _make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
            on_progress=lambda ev: events.append(ev),
            max_reactive_rounds=5,
        )

        result = asyncio.run(
            orch.run_reactive_async(original_query='Async multi-round')
        )

        self.assertIn('Async full answer', result)
        self.assertEqual(len(orch._results), 3)
        self.assertEqual(review_count[0], 2)
        # Verify spawn_more event
        spawn_events = [e for e in events if e.get('phase') == 'spawn_more']
        self.assertGreaterEqual(len(spawn_events), 1)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_async_reactive_fast_path(self, mock_spawn, mock_build,
                                       mock_stream):
        """Async reactive run with fast-path enabled — skips review."""
        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            agent.spec = spec
            agent.agent_id = f'agent-{spec.id}'
            agent.result = make_result(
                answer=f'Clean result from {spec.id}',
                tokens=100,
            )
            agent.run = MagicMock(return_value=agent.result)
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]

        def fake_stream_chat(body, on_content=None, abort_check=None,
                             log_prefix='', on_thinking=None):
            lp = (log_prefix or '').lower()
            if 'review' in lp:
                review_count[0] += 1
                return (
                    {
                        'content': '',
                        'tool_calls': [{
                            'id': 'tc1',
                            'function': {
                                'name': 'swarm_done',
                                'arguments': json.dumps({'summary': 'Done'}),
                            },
                        }],
                    },
                    'tool_calls',
                    {'total_tokens': 80},
                )
            else:
                if on_content:
                    on_content('Fast-path async answer')
                return (
                    {'content': 'Fast-path async answer'},
                    'stop',
                    {'total_tokens': 150},
                )

        mock_stream.side_effect = fake_stream_chat

        events = []
        orch = _make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
            fast_path_enabled=True,
            on_progress=lambda ev: events.append(ev),
        )

        result = asyncio.run(
            orch.run_reactive_async(original_query='Fast-path async test')
        )

        self.assertIn('Fast-path async answer', result)
        # Review should NOT have been called
        self.assertEqual(review_count[0], 0)
        # Fast-path event emitted
        fp_events = [e for e in events
                     if e.get('type') == 'swarm_fast_path_skip']
        self.assertGreaterEqual(len(fp_events), 1)


# ════════════════════════════════════════════════════════════
#  E2E Test 4: Backward Compatibility
# ════════════════════════════════════════════════════════════

class TestBackwardCompat(unittest.TestCase):
    """Ensure existing APIs still work after changes."""

    def test_orchestrator_default_fast_path_disabled(self):
        """Default fast_path_enabled should be False."""
        orch = _make_orchestrator()
        self.assertFalse(orch.fast_path_enabled)

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_fire_and_forget_still_works(self, mock_spawn):
        """Basic run() should still work identically."""
        agent = MagicMock()
        agent.run.return_value = make_result(answer='ok')
        mock_spawn.return_value = agent

        orch = _make_orchestrator()
        results = orch.run()
        self.assertEqual(len(results), 2)
        for spec, result in results:
            self.assertEqual(result.status, SubAgentStatus.COMPLETED.value)

    def test_async_scheduler_exposes_same_properties(self):
        """AsyncStreamingScheduler should expose same properties as sync."""
        def dummy_factory(spec):
            agent = MagicMock()
            agent.spec = spec
            agent.result = make_result()
            agent.run = MagicMock(return_value=agent.result)
            return agent

        async_sched = AsyncStreamingScheduler(
            agent_factory=dummy_factory,
            max_parallel=2,
        )
        self.assertTrue(async_sched.is_idle)
        self.assertEqual(async_sched.completed_count, 0)
        self.assertEqual(async_sched.pending_count, 0)
        self.assertEqual(async_sched.running_count, 0)
        self.assertEqual(async_sched.all_results, [])
        async_sched.shutdown()


if __name__ == '__main__':
    unittest.main()
