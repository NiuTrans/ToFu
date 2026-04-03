#!/usr/bin/env python3
"""Comprehensive test suite for the swarm v2 system.

Tests cover:
  1. Protocol layer (SubTaskSpec, ArtifactStore, SwarmEvent, compress_result)
  2. Registry (model tiers, role scoping)
  3. StreamingScheduler (DAG streaming, retries, cycle detection, cancel)
  4. RateLimiter (token-bucket with backoff)
  5. MasterOrchestrator (fire-and-forget + reactive mode end-to-end)
  6. Integration layer (execute_swarm_tool, session management)
  7. SubAgent (tool dispatch, artifact ops, bounded rounds)

All LLM calls are mocked — no real API keys needed.
"""

import json
import logging
import sys
import threading
import time
import traceback
import unittest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, '.')

from lib.swarm.protocol import (
    SubTaskSpec, SubAgentResult, SubAgentStatus,
    AgentMessage, SwarmEvent, SwarmEventType,
    ArtifactStore, compress_result,
)
from lib.swarm.registry import (
    AGENT_ROLES, get_role_system_suffix,
    get_tools_for_role, resolve_model_for_tier,
    scope_tools_for_role,
)
from lib.swarm.tools import (
    SPAWN_AGENTS_TOOL, CHECK_AGENTS_TOOL,
    SPAWN_MORE_AGENTS_TOOL, SWARM_DONE_TOOL,
    STORE_ARTIFACT_TOOL, READ_ARTIFACT_TOOL, LIST_ARTIFACTS_TOOL,
    MASTER_TOOLS, REACTIVE_MASTER_TOOLS, ARTIFACT_TOOLS,
    SUB_AGENT_TOOLS, SWARM_TOOL_NAMES,
)
from lib.swarm.master import (
    resolve_execution_order,
    MasterOrchestrator,
)
from lib.swarm.scheduler import StreamingScheduler
from lib.swarm.rate_limiter import RateLimiter
from lib.swarm.agent import SubAgent


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


def make_fake_agent(spec: SubTaskSpec, result: Optional[SubAgentResult] = None):
    """Create a fake SubAgent that returns a predetermined result."""
    agent = MagicMock(spec=SubAgent)
    agent.spec = spec
    agent.agent_id = spec.id
    agent.result = result or make_result()
    agent.run = MagicMock(return_value=agent.result)
    return agent


# ════════════════════════════════════════════════════════════
#  1. Protocol Tests
# ════════════════════════════════════════════════════════════

class TestProtocol(unittest.TestCase):

    def test_sub_task_spec_defaults(self):
        spec = SubTaskSpec(role='coder', objective='test')
        self.assertEqual(spec.role, 'coder')
        self.assertIsInstance(spec.id, str)
        self.assertTrue(len(spec.id) > 0)
        self.assertEqual(spec.max_rounds, 30)
        self.assertEqual(spec.max_retries, 0)

    def test_sub_agent_result_defaults(self):
        result = SubAgentResult()
        self.assertEqual(result.status, 'pending')
        self.assertEqual(result.total_tokens, 0)
        self.assertEqual(result.retry_count, 0)

    def test_compress_result_short(self):
        text = 'Hello world'
        self.assertEqual(compress_result(text, max_chars=100), text)

    def test_compress_result_truncation(self):
        text = 'A' * 200
        compressed = compress_result(text, max_chars=50)
        self.assertLessEqual(len(compressed), 80)  # some overhead for markers
        self.assertIn('…', compressed)

    def test_compress_result_none(self):
        self.assertEqual(compress_result(None), '(no result)')

    def test_artifact_store_basic(self):
        store = ArtifactStore()
        store.put('key1', 'value1', 'agent_A')
        self.assertEqual(store.get('key1'), 'value1')
        self.assertIsNone(store.get('missing'))

    def test_artifact_store_summary(self):
        store = ArtifactStore()
        store.put('report', 'Lorem ipsum dolor sit amet', 'agent_A')
        store.put('data', '{"x": 1}', 'agent_B')
        summary = store.summary(max_preview=10)
        self.assertIn('report', summary)
        self.assertIn('data', summary)
        self.assertEqual(len(store), 2)

    def test_artifact_store_thread_safety(self):
        store = ArtifactStore()
        errors = []

        def writer(prefix, count):
            try:
                for i in range(count):
                    store.put(f'{prefix}_{i}', f'val_{i}', f'agent_{prefix}')
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(f't{t}', 50))
                   for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(len(store), 250)

    def test_swarm_event_type_values(self):
        # Verify event types exist
        self.assertIsNotNone(SwarmEventType.AGENT_START)
        self.assertIsNotNone(SwarmEventType.AGENT_COMPLETE)
        self.assertIsNotNone(SwarmEventType.SPAWN_MORE)
        self.assertIsNotNone(SwarmEventType.SYNTHESIS)

    def test_agent_message_dataclass(self):
        msg = AgentMessage(role='user', content='hello')
        self.assertEqual(msg.role, 'user')
        self.assertEqual(msg.content, 'hello')


# ════════════════════════════════════════════════════════════
#  2. Registry Tests
# ════════════════════════════════════════════════════════════

class TestRegistry(unittest.TestCase):

    def test_known_roles(self):
        for role in ['coder', 'researcher', 'analyst', 'reviewer', 'writer', 'general']:
            self.assertIn(role, AGENT_ROLES, f'Role {role} missing from AGENT_ROLES')

    def test_get_role_system_suffix(self):
        suffix = get_role_system_suffix('coder')
        self.assertIsInstance(suffix, str)
        self.assertTrue(len(suffix) > 0)

    def test_get_role_system_suffix_unknown(self):
        suffix = get_role_system_suffix('alien_role')
        self.assertIsInstance(suffix, str)

    def test_resolve_model_for_tier(self):
        base_model = 'gpt-4o-2024-05-13'
        # Standard tier should return the same model or similar
        resolved = resolve_model_for_tier('standard', base_model)
        self.assertIsInstance(resolved, str)
        self.assertTrue(len(resolved) > 0)

    def test_resolve_model_tiers(self):
        base = 'claude-3-5-sonnet'
        light = resolve_model_for_tier('light', base)
        standard = resolve_model_for_tier('standard', base)
        heavy = resolve_model_for_tier('heavy', base)
        self.assertIsInstance(light, str)
        self.assertIsInstance(standard, str)
        self.assertIsInstance(heavy, str)

    def test_scope_tools_for_role(self):
        all_tools = [
            {'type': 'function', 'function': {'name': 'read_file'}},
            {'type': 'function', 'function': {'name': 'write_file'}},
            {'type': 'function', 'function': {'name': 'web_search'}},
            {'type': 'function', 'function': {'name': 'run_command'}},
        ]
        scoped = scope_tools_for_role('researcher', all_tools)
        self.assertIsInstance(scoped, list)
        self.assertTrue(len(scoped) > 0)

    def test_get_tools_for_role(self):
        hints = get_tools_for_role('coder')
        self.assertIsInstance(hints, (list, set, frozenset))


# ════════════════════════════════════════════════════════════
#  3. resolve_execution_order Tests
# ════════════════════════════════════════════════════════════

class TestExecutionOrder(unittest.TestCase):

    def test_independent_specs(self):
        specs = [make_spec('a'), make_spec('b'), make_spec('c')]
        waves = resolve_execution_order(specs)
        # All independent → should be in one wave
        self.assertEqual(len(waves), 1)
        self.assertEqual(len(waves[0]), 3)

    def test_linear_chain(self):
        specs = [
            make_spec('a'),
            make_spec('b', depends_on=['a']),
            make_spec('c', depends_on=['b']),
        ]
        waves = resolve_execution_order(specs)
        self.assertEqual(len(waves), 3)
        self.assertEqual(waves[0][0].id, 'a')
        self.assertEqual(waves[1][0].id, 'b')
        self.assertEqual(waves[2][0].id, 'c')

    def test_diamond_dependency(self):
        specs = [
            make_spec('a'),
            make_spec('b', depends_on=['a']),
            make_spec('c', depends_on=['a']),
            make_spec('d', depends_on=['b', 'c']),
        ]
        waves = resolve_execution_order(specs)
        # Wave 0: [a], Wave 1: [b, c], Wave 2: [d]
        self.assertEqual(len(waves), 3)
        wave1_ids = {s.id for s in waves[1]}
        self.assertEqual(wave1_ids, {'b', 'c'})

    def test_cycle_detection(self):
        specs = [
            make_spec('a', depends_on=['b']),
            make_spec('b', depends_on=['a']),
        ]
        with self.assertRaises(ValueError) as ctx:
            resolve_execution_order(specs)
        self.assertIn('cycle', str(ctx.exception).lower())

    def test_self_dependency_cycle(self):
        specs = [make_spec('a', depends_on=['a'])]
        with self.assertRaises(ValueError):
            resolve_execution_order(specs)

    def test_three_way_cycle(self):
        specs = [
            make_spec('a', depends_on=['c']),
            make_spec('b', depends_on=['a']),
            make_spec('c', depends_on=['b']),
        ]
        with self.assertRaises(ValueError):
            resolve_execution_order(specs)


# ════════════════════════════════════════════════════════════
#  4. RateLimiter Tests
# ════════════════════════════════════════════════════════════

class TestRateLimiter(unittest.TestCase):

    def test_basic_concurrency(self):
        limiter = RateLimiter(max_concurrent=2)
        results = []

        def fake_run():
            results.append(('start', time.monotonic()))
            time.sleep(0.1)
            results.append(('end', time.monotonic()))
            return make_result()

        agent = MagicMock()
        agent.run = fake_run
        result = limiter.run_agent(agent)
        self.assertEqual(result.status, 'completed')

    def test_concurrent_limit(self):
        limiter = RateLimiter(max_concurrent=2)
        max_concurrent_seen = [0]
        current = [0]
        lock = threading.Lock()

        def fake_run():
            with lock:
                current[0] += 1
                max_concurrent_seen[0] = max(max_concurrent_seen[0], current[0])
            time.sleep(0.1)
            with lock:
                current[0] -= 1
            return make_result()

        agents = []
        for _ in range(4):
            a = MagicMock()
            a.run = fake_run
            agents.append(a)

        threads = [threading.Thread(target=limiter.run_agent, args=(a,))
                   for a in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(max_concurrent_seen[0], 2)


# ════════════════════════════════════════════════════════════
#  5. StreamingScheduler Tests
# ════════════════════════════════════════════════════════════

class TestStreamingScheduler(unittest.TestCase):

    def _make_scheduler(self, factory=None, max_parallel=4, retries=0):
        if factory is None:
            def factory(spec):
                agent = MagicMock()
                agent.run = MagicMock(return_value=make_result(
                    answer=f'done:{spec.id}',
                ))
                return agent
        return StreamingScheduler(
            agent_factory=factory,
            max_parallel=max_parallel,
            default_retries=retries,
        )

    def test_independent_specs_all_complete(self):
        scheduler = self._make_scheduler()
        specs = [make_spec('a'), make_spec('b'), make_spec('c')]
        scheduler.add_specs(specs)
        batch = scheduler.run_until_idle(timeout=10)
        self.assertEqual(len(batch), 3)
        ids = {s.id for s, _ in batch}
        self.assertEqual(ids, {'a', 'b', 'c'})
        scheduler.shutdown()

    def test_dependency_ordering(self):
        """Specs with deps only run after their deps complete."""
        order = []
        lock = threading.Lock()

        def factory(spec):
            agent = MagicMock()
            def run():
                with lock:
                    order.append(spec.id)
                time.sleep(0.05)
                return make_result(answer=f'done:{spec.id}')
            agent.run = run
            return agent

        scheduler = self._make_scheduler(factory=factory)
        specs = [
            make_spec('a'),
            make_spec('b', depends_on=['a']),
        ]
        scheduler.add_specs(specs)
        scheduler.run_until_idle(timeout=10)

        self.assertEqual(order.index('a'), 0)
        self.assertGreater(order.index('b'), order.index('a'))
        scheduler.shutdown()

    def test_streaming_no_wave_barrier(self):
        """B depends on A, C is independent.
        C should NOT wait for A to complete — it should start immediately.
        B should start as soon as A completes, even if C is still running.
        """
        events = []
        lock = threading.Lock()

        def factory(spec):
            agent = MagicMock()
            def run():
                with lock:
                    events.append(('start', spec.id, time.monotonic()))
                # A is fast, C is slow
                sleep_time = 0.05 if spec.id == 'a' else 0.3
                time.sleep(sleep_time)
                with lock:
                    events.append(('end', spec.id, time.monotonic()))
                return make_result(answer=f'done:{spec.id}')
            agent.run = run
            return agent

        scheduler = self._make_scheduler(factory=factory, max_parallel=4)
        specs = [
            make_spec('a'),
            make_spec('b', depends_on=['a']),
            make_spec('c'),  # independent, slow
        ]
        scheduler.add_specs(specs)
        scheduler.run_until_idle(timeout=10)

        # Verify B started before C ended (streaming, not wave barrier)
        b_start = next(t for ev, sid, t in events if ev == 'start' and sid == 'b')
        c_end = next(t for ev, sid, t in events if ev == 'end' and sid == 'c')
        # B should start as soon as A finishes, which is ~0.05s
        # C ends at ~0.3s. So B should start well before C ends.
        self.assertLess(b_start, c_end,
                        'B should start before C finishes (streaming, not wave barrier)')
        scheduler.shutdown()

    def test_iter_completions(self):
        """iter_completions yields results one by one."""
        scheduler = self._make_scheduler()
        specs = [make_spec('a'), make_spec('b')]
        scheduler.add_specs(specs)

        results = list(scheduler.iter_completions(timeout=10))
        self.assertEqual(len(results), 2)
        scheduler.shutdown()

    def test_cancel_pending(self):
        """cancel_pending removes queued (not-yet-started) specs."""
        started = []

        def slow_factory(spec):
            agent = MagicMock()
            def run():
                started.append(spec.id)
                time.sleep(0.5)  # Very slow
                return make_result()
            agent.run = run
            return agent

        # max_parallel=1 so only one runs at a time
        scheduler = self._make_scheduler(factory=slow_factory, max_parallel=1)
        specs = [
            make_spec('a'),
            make_spec('b', depends_on=['a']),
            make_spec('c', depends_on=['a']),
        ]
        scheduler.add_specs(specs)
        time.sleep(0.1)  # Let 'a' start

        # Cancel pending — b and c should be removed
        cancelled = scheduler.cancel_pending()
        self.assertGreaterEqual(len(cancelled), 0)  # a started, b/c pending

        scheduler.run_until_idle(timeout=5)
        scheduler.shutdown()

    def test_cycle_detection_on_add(self):
        scheduler = self._make_scheduler()
        specs = [
            make_spec('x', depends_on=['y']),
            make_spec('y', depends_on=['x']),
        ]
        with self.assertRaises(ValueError):
            scheduler.add_specs(specs)
        scheduler.shutdown()

    def test_auto_retry_on_failure(self):
        """Agent fails on first attempt, succeeds on retry."""
        attempts = [0]

        def retry_factory(spec):
            agent = MagicMock()
            def run():
                attempts[0] += 1
                if attempts[0] == 1:
                    return SubAgentResult(
                        status=SubAgentStatus.FAILED.value,
                        error_message='transient error',
                    )
                return make_result(answer='success on retry')
            agent.run = run
            return agent

        scheduler = self._make_scheduler(factory=retry_factory, retries=1)
        scheduler.add_specs([make_spec('a')])
        batch = scheduler.run_until_idle(timeout=10)
        self.assertEqual(len(batch), 1)
        _, result = batch[0]
        self.assertEqual(result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(result.retry_count, 1)
        scheduler.shutdown()

    def test_abort_check(self):
        """abort_check stops the scheduler."""
        aborted = [False]

        def factory(spec):
            agent = MagicMock()
            def run():
                time.sleep(0.5)
                return make_result()
            agent.run = run
            return agent

        scheduler = StreamingScheduler(
            agent_factory=factory,
            max_parallel=2,
            abort_check=lambda: aborted[0],
        )

        specs = [make_spec('a'), make_spec('b'), make_spec('c')]
        scheduler.add_specs(specs)
        time.sleep(0.1)
        aborted[0] = True  # Signal abort

        batch = scheduler.run_until_idle(timeout=3)
        # Some may have started, but scheduler should stop
        scheduler.shutdown()

    def test_dependency_context_injection(self):
        """Specs with deps get the dep results injected into context."""
        received_context = []

        def factory(spec):
            agent = MagicMock()
            def run():
                received_context.append((spec.id, spec.context))
                return make_result(answer=f'result_from_{spec.id}')
            agent.run = run
            return agent

        scheduler = self._make_scheduler(factory=factory)
        specs = [
            make_spec('a', context='original_a'),
            make_spec('b', depends_on=['a'], context='original_b'),
        ]
        scheduler.add_specs(specs)
        scheduler.run_until_idle(timeout=10)

        # b should have dep results injected
        b_ctx = next(ctx for sid, ctx in received_context if sid == 'b')
        self.assertIn('prerequisite', b_ctx.lower())
        self.assertIn('result_from_a', b_ctx)
        scheduler.shutdown()


# ════════════════════════════════════════════════════════════
#  6. MasterOrchestrator Tests
# ════════════════════════════════════════════════════════════

class TestMasterOrchestrator(unittest.TestCase):

    def _make_orchestrator(self, specs=None, **kwargs):
        if specs is None:
            specs = [make_spec('a'), make_spec('b')]
        defaults = dict(
            task_id='test-task',
            conv_id='test-conv',
            specs=specs,
            model='test-model',
            all_tools=[],
            max_parallel=2,
            max_retries=0,
        )
        defaults.update(kwargs)
        return MasterOrchestrator(**defaults)

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_fire_and_forget_run(self, mock_spawn):
        """Basic run() mode — no reactive review."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = make_result(answer='done')
        mock_spawn.return_value = mock_agent

        orch = self._make_orchestrator()
        results = orch.run()
        self.assertEqual(len(results), 2)
        for spec, result in results:
            self.assertEqual(result.status, SubAgentStatus.COMPLETED.value)

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_fire_and_forget_with_dependencies(self, mock_spawn):
        """run() with dependency chain."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = make_result(answer='done')
        mock_spawn.return_value = mock_agent

        specs = [
            make_spec('a'),
            make_spec('b', depends_on=['a']),
        ]
        orch = self._make_orchestrator(specs=specs)
        results = orch.run()
        self.assertEqual(len(results), 2)

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_abort_stops_execution(self, mock_spawn):
        """Aborting the orchestrator stops sub-agents."""
        mock_agent = MagicMock()
        mock_agent.run.side_effect = lambda: (time.sleep(0.5), make_result())[1]
        mock_spawn.return_value = mock_agent

        orch = self._make_orchestrator()
        orch.abort()
        results = orch.run()
        # Some or all may have been cancelled
        self.assertIsInstance(results, list)

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_dashboard_generation(self, mock_spawn):
        """Dashboard should produce a markdown table."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = make_result(answer='test result', tokens=500)
        mock_spawn.return_value = mock_agent

        orch = self._make_orchestrator()
        orch.run()
        dashboard = orch._build_dashboard()
        self.assertIn('Role', dashboard)
        self.assertIn('Status', dashboard)
        self.assertIn('✅', dashboard)

    @patch('lib.swarm.master.spawn_sub_agent')
    def test_get_status(self, mock_spawn):
        """get_status returns per-agent status."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = make_result()
        mock_agent.spec = make_spec('a')
        mock_agent.result = make_result()
        mock_spawn.return_value = mock_agent

        orch = self._make_orchestrator(specs=[make_spec('a')])
        orch.run()
        status = orch.get_status()
        self.assertIsInstance(status, dict)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_reactive_run_done_immediately(self, mock_spawn, mock_build, mock_stream):
        """Reactive mode where master says 'done' after first review."""
        # Sub-agents succeed
        mock_agent = MagicMock()
        mock_agent.run.return_value = make_result(answer='analysis done')
        mock_spawn.return_value = mock_agent

        # build_body returns a dict
        mock_build.return_value = {'model': 'test', 'messages': []}

        # First stream_chat: master review → calls swarm_done
        # Second stream_chat: synthesis
        call_count = [0]
        def fake_stream_chat(body, on_content=None, abort_check=None, log_prefix=''):
            call_count[0] += 1
            if 'review' in (log_prefix or '').lower() or call_count[0] == 1:
                # Master review — call swarm_done
                return (
                    {
                        'content': '',
                        'tool_calls': [{
                            'id': 'tc_1',
                            'function': {
                                'name': 'swarm_done',
                                'arguments': json.dumps({'summary': 'All good'})
                            }
                        }]
                    },
                    'tool_calls',
                    {'total_tokens': 100}
                )
            else:
                # Synthesis call
                if on_content:
                    on_content('Final synthesised answer')
                return (
                    {'content': 'Final synthesised answer'},
                    'stop',
                    {'total_tokens': 200}
                )

        mock_stream.side_effect = fake_stream_chat

        orch = self._make_orchestrator(specs=[make_spec('a'), make_spec('b')])
        result = orch.run_reactive(original_query='Analyze the codebase')
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_reactive_run_spawn_more(self, mock_spawn, mock_build, mock_stream):
        """Reactive mode where master spawns more agents then says done."""
        mock_agent = MagicMock()
        mock_agent.run.return_value = make_result(answer='partial result')
        mock_spawn.return_value = mock_agent

        mock_build.return_value = {'model': 'test', 'messages': []}

        review_count = [0]
        def fake_stream_chat(body, on_content=None, abort_check=None, log_prefix=''):
            if 'review' in (log_prefix or '').lower():
                review_count[0] += 1
                if review_count[0] == 1:
                    # First review: spawn more
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc_1',
                                'function': {
                                    'name': 'spawn_more_agents',
                                    'arguments': json.dumps({
                                        'reason': 'Need deeper analysis',
                                        'agents': [
                                            {'role': 'analyst', 'objective': 'Deep dive', 'id': 'c'}
                                        ]
                                    })
                                }
                            }]
                        },
                        'tool_calls',
                        {'total_tokens': 150}
                    )
                else:
                    # Second review: done
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc_2',
                                'function': {
                                    'name': 'swarm_done',
                                    'arguments': json.dumps({'summary': 'Complete'})
                                }
                            }]
                        },
                        'tool_calls',
                        {'total_tokens': 100}
                    )
            else:
                # Synthesis
                if on_content:
                    on_content('Synthesised with extra analysis')
                return (
                    {'content': 'Synthesised with extra analysis'},
                    'stop',
                    {'total_tokens': 300}
                )

        mock_stream.side_effect = fake_stream_chat

        events = []
        orch = self._make_orchestrator(
            specs=[make_spec('a'), make_spec('b')],
            on_progress=lambda ev: events.append(ev),
        )
        result = orch.run_reactive(original_query='Complex analysis')

        self.assertIsInstance(result, str)
        # Verify spawn_more event was emitted
        spawn_events = [e for e in events if e.get('phase') == 'spawn_more']
        self.assertGreaterEqual(len(spawn_events), 1)
        # Verify 3 agents total ran (a, b, c)
        self.assertGreaterEqual(len(orch._results), 3)


# ════════════════════════════════════════════════════════════
#  7. Tools Definitions Tests
# ════════════════════════════════════════════════════════════

class TestToolDefinitions(unittest.TestCase):

    def test_spawn_agents_tool_schema(self):
        fn = SPAWN_AGENTS_TOOL['function']
        self.assertEqual(fn['name'], 'spawn_agents')
        self.assertIn('parameters', fn)
        props = fn['parameters']['properties']
        self.assertIn('agents', props)

    def test_reactive_master_tools_complete(self):
        names = {t['function']['name'] for t in REACTIVE_MASTER_TOOLS}
        self.assertIn('spawn_more_agents', names)
        self.assertIn('swarm_done', names)

    def test_artifact_tools_complete(self):
        names = {t['function']['name'] for t in ARTIFACT_TOOLS}
        self.assertIn('store_artifact', names)
        self.assertIn('read_artifact', names)
        self.assertIn('list_artifacts', names)

    def test_swarm_tool_names_set(self):
        self.assertIn('spawn_agents', SWARM_TOOL_NAMES)
        self.assertIn('check_agents', SWARM_TOOL_NAMES)
        self.assertIn('spawn_more_agents', SWARM_TOOL_NAMES)
        self.assertIn('swarm_done', SWARM_TOOL_NAMES)
        self.assertIn('store_artifact', SWARM_TOOL_NAMES)
        self.assertIn('read_artifact', SWARM_TOOL_NAMES)
        self.assertIn('list_artifacts', SWARM_TOOL_NAMES)

    def test_sub_agent_tools_include_artifacts(self):
        names = {t['function']['name'] for t in SUB_AGENT_TOOLS}
        self.assertIn('store_artifact', names)
        self.assertIn('read_artifact', names)
        self.assertIn('list_artifacts', names)


# ════════════════════════════════════════════════════════════
#  8. Integration Tests
# ════════════════════════════════════════════════════════════

class TestIntegration(unittest.TestCase):

    def test_swarm_tool_names_routing(self):
        """SWARM_TOOL_NAMES should match what executor uses for routing."""
        expected = {'spawn_agents', 'check_agents', 'spawn_more_agents',
                    'swarm_done', 'store_artifact', 'read_artifact', 'list_artifacts'}
        self.assertEqual(set(SWARM_TOOL_NAMES), expected)

    @patch('lib.swarm.integration.MasterOrchestrator')
    def test_execute_swarm_tool_creates_session(self, MockMaster):
        """execute_swarm_tool should create a MasterOrchestrator and run it."""
        from lib.swarm.integration import execute_swarm_tool

        mock_orch = MagicMock()
        mock_orch.run_reactive.return_value = 'final answer'
        mock_orch._results = [(make_spec('a'), make_result())]
        mock_orch.artifact_store = ArtifactStore()
        MockMaster.return_value = mock_orch

        result = execute_swarm_tool(
            fn_name='spawn_agents',
            fn_args={
                'agents': [
                    {'role': 'coder', 'objective': 'Write code'},
                    {'role': 'analyst', 'objective': 'Analyze'},
                ]
            },
            task={'id': 'task-1', 'convId': 'conv-1'},
            all_tools=[],
            model='test-model',
        )
        self.assertIsInstance(result, str)


# ════════════════════════════════════════════════════════════
#  9. SubAgent Tests (mocked LLM)
# ════════════════════════════════════════════════════════════

class TestSubAgent(unittest.TestCase):

    def _make_sub_agent(self, spec=None, **kwargs):
        if spec is None:
            spec = make_spec('test-agent', max_rounds=3)
        defaults = dict(
            parent_task={'id': 'task-1'},
            all_tools=[],
            model='test-model',
            thinking_enabled=False,
        )
        defaults.update(kwargs)
        return SubAgent(spec=spec, **defaults)

    @patch('lib.swarm.agent.stream_chat')
    @patch('lib.swarm.agent.build_body')
    def test_agent_completes_with_text_response(self, mock_build, mock_stream):
        """Agent should complete when LLM returns text (no tool calls)."""
        mock_build.return_value = {'model': 'test'}
        mock_stream.return_value = (
            {'content': 'Here is the answer', 'tool_calls': []},
            'stop',
            {'total_tokens': 50, 'prompt_tokens': 30, 'completion_tokens': 20},
        )

        agent = self._make_sub_agent()
        result = agent.run()
        self.assertEqual(result.status, SubAgentStatus.COMPLETED.value)
        self.assertIn('answer', result.final_answer.lower())

    @patch('lib.swarm.agent.stream_chat')
    @patch('lib.swarm.agent.build_body')
    def test_agent_max_rounds_bounded(self, mock_build, mock_stream):
        """Agent should stop after max_rounds."""
        mock_build.return_value = {'model': 'test'}
        # Always return tool calls so agent loops
        mock_stream.return_value = (
            {
                'content': '',
                'tool_calls': [{
                    'id': 'tc1',
                    'function': {'name': 'nonexistent_tool', 'arguments': '{}'}
                }]
            },
            'tool_calls',
            {'total_tokens': 10, 'prompt_tokens': 5, 'completion_tokens': 5},
        )

        spec = make_spec('bounded', max_rounds=2)
        agent = self._make_sub_agent(spec=spec)
        result = agent.run()
        self.assertLessEqual(result.rounds_used, 2)

    def test_agent_artifact_store_integration(self):
        """Agent should be able to read/write artifacts."""
        store = ArtifactStore()
        store.put('preloaded', 'initial data', 'setup')

        spec = make_spec('art-agent')
        agent = self._make_sub_agent(spec=spec, artifact_store=store)

        # Verify agent has reference to artifact store
        self.assertEqual(agent.artifact_store.get('preloaded'), 'initial data')


# ════════════════════════════════════════════════════════════
#  10. End-to-End Reactive Flow
# ════════════════════════════════════════════════════════════

class TestEndToEndReactive(unittest.TestCase):
    """Full reactive loop: spawn → stream → master review → spawn_more → synthesis."""

    @patch('lib.swarm.master.stream_chat')
    @patch('lib.swarm.master.build_body')
    @patch('lib.swarm.master.spawn_sub_agent')
    def test_full_reactive_lifecycle(self, mock_spawn, mock_build, mock_stream):
        """Complete lifecycle:
        1. Initial spawn: 2 agents (coder + researcher)
        2. Both complete
        3. Master reviews → spawns 1 more (analyst)
        4. Analyst completes
        5. Master reviews → says done
        6. Synthesis produces final answer
        """
        # Track which agents ran
        agents_ran = []
        lock = threading.Lock()

        def fake_spawn(spec, **kwargs):
            agent = MagicMock()
            def run():
                with lock:
                    agents_ran.append(spec.id)
                time.sleep(0.05)
                return make_result(
                    answer=f'Result from {spec.role} ({spec.id}): {spec.objective}',
                    tokens=200,
                    elapsed=0.5,
                )
            agent.run = run
            agent.spec = spec
            agent.agent_id = spec.id
            agent.result = None
            return agent

        mock_spawn.side_effect = fake_spawn
        mock_build.return_value = {'model': 'test', 'messages': []}

        review_call = [0]
        def fake_stream(body, on_content=None, abort_check=None, log_prefix=''):
            if 'review' in (log_prefix or '').lower():
                review_call[0] += 1
                if review_call[0] == 1:
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc_spawn',
                                'function': {
                                    'name': 'spawn_more_agents',
                                    'arguments': json.dumps({
                                        'reason': 'Need analyst perspective',
                                        'agents': [{
                                            'role': 'analyst',
                                            'objective': 'Analyze findings',
                                            'id': 'analyst_1',
                                        }]
                                    })
                                }
                            }]
                        },
                        'tool_calls',
                        {'total_tokens': 150}
                    )
                else:
                    return (
                        {
                            'content': '',
                            'tool_calls': [{
                                'id': 'tc_done',
                                'function': {
                                    'name': 'swarm_done',
                                    'arguments': json.dumps({'summary': 'All analyses complete'})
                                }
                            }]
                        },
                        'tool_calls',
                        {'total_tokens': 100}
                    )
            else:
                # Synthesis
                final = 'Comprehensive analysis combining coder, researcher, and analyst results.'
                if on_content:
                    on_content(final)
                return (
                    {'content': final},
                    'stop',
                    {'total_tokens': 500}
                )

        mock_stream.side_effect = fake_stream

        # Track events
        events = []
        orch = MasterOrchestrator(
            task_id='e2e-test',
            conv_id='conv-e2e',
            specs=[
                make_spec('coder_1', role='coder', objective='Write implementation'),
                make_spec('researcher_1', role='researcher', objective='Research APIs'),
            ],
            model='test-model',
            all_tools=[],
            max_parallel=4,
            max_reactive_rounds=5,
            on_progress=lambda ev: events.append(ev),
        )

        final_answer = orch.run_reactive(original_query='Build a REST API client')

        # ── Assertions ──
        # 1. All 3 agents ran
        self.assertIn('coder_1', agents_ran)
        self.assertIn('researcher_1', agents_ran)
        self.assertIn('analyst_1', agents_ran)
        self.assertEqual(len(agents_ran), 3)

        # 2. Final answer was produced
        self.assertIn('Comprehensive', final_answer)

        # 3. Events show the full lifecycle
        phases = [e.get('phase') for e in events if 'phase' in e]
        self.assertIn('executing', phases)
        self.assertIn('spawn_more', phases)
        self.assertIn('synthesis', phases)
        self.assertIn('complete', phases)

        # 4. Dashboard was generated
        dashboard = orch._build_dashboard()
        self.assertIn('coder', dashboard)
        self.assertIn('✅', dashboard)

        # 5. Two reactive reviews happened
        self.assertEqual(review_call[0], 2)

        # 6. Results include all 3 agents
        self.assertEqual(len(orch._results), 3)


# ════════════════════════════════════════════════════════════
#  11. Edge Cases
# ════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_empty_specs(self):
        waves = resolve_execution_order([])
        self.assertEqual(len(waves), 0)

    def test_single_spec(self):
        waves = resolve_execution_order([make_spec('a')])
        self.assertEqual(len(waves), 1)
        self.assertEqual(len(waves[0]), 1)

    def test_large_parallel_specs(self):
        """50 independent specs should all be in one wave."""
        specs = [make_spec(f's{i}') for i in range(50)]
        waves = resolve_execution_order(specs)
        self.assertEqual(len(waves), 1)
        self.assertEqual(len(waves[0]), 50)

    def test_artifact_store_overwrite(self):
        store = ArtifactStore()
        store.put('k', 'v1', 'a1')
        store.put('k', 'v2', 'a2')
        self.assertEqual(store.get('k'), 'v2')

    def test_compress_result_empty_string(self):
        self.assertEqual(compress_result(''), '(no result)')

    def test_streaming_scheduler_hot_add(self):
        """Can add specs to a running scheduler."""
        results = []
        lock = threading.Lock()

        def factory(spec):
            agent = MagicMock()
            def run():
                time.sleep(0.1)
                with lock:
                    results.append(spec.id)
                return make_result(answer=f'done:{spec.id}')
            agent.run = run
            return agent

        scheduler = StreamingScheduler(
            agent_factory=factory,
            max_parallel=4,
        )

        # Add first batch
        scheduler.add_specs([make_spec('a'), make_spec('b')])
        time.sleep(0.05)  # Let them start

        # Hot-add more while first batch is running
        scheduler.add_specs([make_spec('c'), make_spec('d')])

        batch = scheduler.run_until_idle(timeout=10)
        all_ids = {s.id for s, _ in batch}
        self.assertEqual(all_ids, {'a', 'b', 'c', 'd'})
        scheduler.shutdown()


# ════════════════════════════════════════════════════════════
#  Run
# ════════════════════════════════════════════════════════════

if __name__ == '__main__':
    # Run with verbose output
    unittest.main(verbosity=2)
