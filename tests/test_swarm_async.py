"""tests/test_swarm_async.py — Async swarm protocol integration tests.

Covers:
  * ``spawn_agents`` returns a handle immediately (no blocking)
  * Sub-agent completions enqueue ``<swarm-update>`` payloads to ``agent_inbox``
  * ``await_agents`` blocks until ≥1 / all complete and returns batch
  * ``get_agent_result`` returns the full body, or status notice
  * ``orchestrator`` between-round drain hook (covered indirectly: we
    confirm ``agent_inbox.drain(task_id)`` returns the queued items)
  * Sub-agent tool denylist still works on every role
"""

from __future__ import annotations

import json
import time
import unittest
from unittest.mock import patch

from lib import agent_inbox
from lib.swarm.integration import (
    _active_sessions,
    _sessions_lock,
    execute_swarm_tool,
)
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import (
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
)


# ─────────────────────────────────────────────────────────
#  Fake SubAgent — returned by patched factory; bypasses LLM
# ─────────────────────────────────────────────────────────

class _FakeAgent:
    """Stand-in for SubAgent that finishes immediately with a canned answer."""

    def __init__(self, spec: SubTaskSpec, *,
                 final_answer: str = '',
                 elapsed: float = 0.05,
                 tokens: int = 100,
                 status: str = SubAgentStatus.COMPLETED.value):
        self.spec = spec
        self.agent_id = f'agent-{spec.role}-{spec.id}'
        self.result = SubAgentResult(
            status=status,
            final_answer=final_answer or f'Answer for {spec.id}: {spec.objective[:30]}',
            elapsed_seconds=elapsed,
            total_tokens=tokens,
            rounds_used=1,
        )
        self.max_rounds = spec.max_rounds

    def run(self) -> SubAgentResult:
        # Simulate a tiny bit of work so the scheduler thread can yield.
        time.sleep(0.01)
        return self.result


def _patch_factory(fake_results: dict[str, dict] | None = None):
    """Return a patcher that makes _build_sub_agent produce _FakeAgent."""
    fake_results = fake_results or {}

    def _factory(spec, **kwargs):
        cfg = fake_results.get(spec.id, {})
        return _FakeAgent(spec, **cfg)

    return patch('lib.swarm.master._build_sub_agent', side_effect=_factory)


def _wait_until(predicate, timeout=2.0, poll=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(poll)
    return False


# ─────────────────────────────────────────────────────────
#  Fixture-style helpers
# ─────────────────────────────────────────────────────────

def _reset_global_state(task_id: str):
    """Clear any leaking session / inbox / tombstones between tests."""
    with _sessions_lock:
        _active_sessions.pop(task_id, None)
    agent_inbox.reset_for_test(task_id)


# ═════════════════════════════════════════════════════════
#  spawn_agents — handle is non-blocking
# ═════════════════════════════════════════════════════════

class TestSpawnReturnsHandle(unittest.TestCase):

    def setUp(self):
        self.task_id = 'tspawn-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_spawn_returns_handle_immediately(self):
        with _patch_factory():
            t0 = time.time()
            raw = execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'objective': 'Investigate A', 'role': 'researcher'},
                    {'objective': 'Investigate B', 'role': 'researcher'},
                ]},
                task={'id': self.task_id},
            )
            elapsed = time.time() - t0

        handle = json.loads(raw)
        self.assertEqual(handle['status'], 'async_launched')
        self.assertEqual(handle['swarm_id'], self.task_id)
        self.assertEqual(len(handle['agents']), 2)
        self.assertIn('output_file', handle['agents'][0])
        # Should return well under a second — no blocking on LLM
        self.assertLess(elapsed, 2.0,
                         f'spawn_agents blocked for {elapsed:.2f}s — must return immediately')

    def test_spawn_with_no_agents_returns_error(self):
        raw = execute_swarm_tool('spawn_agents', {'agents': []},
                                  task={'id': self.task_id})
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'error')

    def test_cycle_detection_returns_error(self):
        raw = execute_swarm_tool(
            'spawn_agents',
            {'agents': [
                {'id': 'a', 'objective': 'A', 'depends_on': ['b']},
                {'id': 'b', 'objective': 'B', 'depends_on': ['a']},
            ]},
            task={'id': self.task_id},
        )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'error')
        self.assertIn('Cycle', payload['error'])


# ═════════════════════════════════════════════════════════
#  Inbox receives <swarm-update> on completion
# ═════════════════════════════════════════════════════════

class TestInboxReceivesUpdates(unittest.TestCase):

    def setUp(self):
        self.task_id = 'tinbox-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_completion_enqueues_swarm_update(self):
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'aa', 'objective': 'Investigate A'},
                    {'id': 'bb', 'objective': 'Investigate B'},
                ]},
                task={'id': self.task_id},
            )
            self.assertTrue(
                _wait_until(lambda: agent_inbox.peek(self.task_id) >= 2),
                'expected 2 swarm-update items in inbox',
            )

        items = agent_inbox.drain(self.task_id)
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertEqual(it['mode'], 'swarm-update')
            self.assertEqual(it['priority'], 'later')
            self.assertIn(it['agent_id'], ('aa', 'bb'))
            self.assertIn('<swarm-update>', it['value'])
            self.assertIn('<status>completed</status>', it['value'])
            self.assertIn('<preview>', it['value'])

    def test_inbox_keyed_by_full_task_id_not_8char_prefix(self):
        """Regression: the orchestrator drained with ``task['id'][:8]``
        (the log prefix ``tid``) while master.py enqueues — and the
        task-end clear wipes — under the FULL ``task['id']``.  The 8-char
        key never matched, so every <swarm-update> sat unread and was
        silently discarded at task end (0 injects, N cleared-unread per
        session in app.log).  Guard the contract: the queue lives under
        the full id, and the truncated prefix finds nothing."""
        full_id = 'mpwfpik41m95mx-' + 'deadbeef0123456789'  # >8 chars, UUID-like
        _reset_global_state(full_id)
        try:
            with _patch_factory():
                execute_swarm_tool(
                    'spawn_agents',
                    {'agents': [{'id': 'zz', 'objective': 'Investigate Z'}]},
                    task={'id': full_id},
                )
                self.assertTrue(
                    _wait_until(lambda: agent_inbox.peek(full_id) >= 1),
                    'expected swarm-update enqueued under FULL task id',
                )
            # The truncated prefix the orchestrator USED to drain with
            # must NOT see the items — proving the prefix bug would lose them.
            self.assertEqual(agent_inbox.peek(full_id[:8]), 0,
                             '8-char prefix must not collide with the full-id queue')
            # Draining with the full id (what the orchestrator does now)
            # returns the queued items.
            items = agent_inbox.drain(full_id)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['agent_id'], 'zz')
        finally:
            _reset_global_state(full_id)
            _reset_global_state(full_id[:8])


# ═════════════════════════════════════════════════════════
#  await_agents
# ═════════════════════════════════════════════════════════

class TestAwaitAgents(unittest.TestCase):

    def setUp(self):
        self.task_id = 'tawait-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_await_all_returns_when_done(self):
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'x', 'objective': 'X'},
                    {'id': 'y', 'objective': 'Y'},
                ]},
                task={'id': self.task_id},
            )

            raw = execute_swarm_tool(
                'await_agents',
                {'mode': 'all', 'timeout_seconds': 5},
                task={'id': self.task_id},
            )

        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['timed_out'])
        ids_seen = {p['agent_id'] for p in result['completed']}
        self.assertEqual(ids_seen, {'x', 'y'})

    def test_await_no_session_returns_error(self):
        raw = execute_swarm_tool(
            'await_agents', {},
            task={'id': self.task_id},
        )
        self.assertEqual(json.loads(raw)['status'], 'error')

    def test_await_timeout_clamped_to_hard_cap(self):
        # Hard cap is 120s; passing 99999 must clamp without crashing.
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'q', 'objective': 'Q'}]},
                task={'id': self.task_id},
            )
            raw = execute_swarm_tool(
                'await_agents',
                {'timeout_seconds': 99999},
                task={'id': self.task_id},
            )
        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')

    def test_await_already_done_id_returns_immediately(self):
        """B1: await_agents asked for an id that's ALREADY done should
        return its result immediately, not block-then-timeout."""
        with _patch_factory({'a1': {'final_answer': 'done already'}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 1,
            ))
            t0 = time.time()
            raw = execute_swarm_tool(
                'await_agents',
                {'ids': ['a1'], 'mode': 'all', 'timeout_seconds': 5},
                task={'id': self.task_id},
            )
            elapsed = time.time() - t0
        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['timed_out'], 'should not have hit timeout')
        self.assertLess(elapsed, 1.0,
                         f'should return immediately; took {elapsed:.2f}s')
        self.assertEqual(len(result['completed']), 1)
        self.assertEqual(result['completed'][0]['agent_id'], 'a1')

    def test_await_unknown_id_returns_unknown_field(self):
        """B1: await_agents with unknown id returns it in 'unknown'."""
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'real', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            raw = execute_swarm_tool(
                'await_agents',
                {'ids': ['real', 'phantom'], 'mode': 'all', 'timeout_seconds': 5},
                task={'id': self.task_id},
            )
        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')
        self.assertIn('unknown', result)
        self.assertIn('phantom', result['unknown'])

    def test_await_consumes_inbox_for_returned_agents(self):
        """De-dup: agents returned synchronously by await_agents must NOT
        also remain in the inbox (which would re-inject them as a duplicate
        <swarm-update> on the next round)."""
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'x', 'objective': 'X'},
                    {'id': 'y', 'objective': 'Y'},
                ]},
                task={'id': self.task_id},
            )
            # Let both finish and land in the inbox.
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 2))
            raw = execute_swarm_tool(
                'await_agents',
                {'ids': ['x', 'y'], 'mode': 'all', 'timeout_seconds': 5},
                task={'id': self.task_id},
            )
        result = json.loads(raw)
        self.assertEqual({p['agent_id'] for p in result['completed']}, {'x', 'y'})
        # Both were returned in the tool result → inbox must now be empty.
        self.assertEqual(agent_inbox.peek(self.task_id), 0,
                         'await_agents did not consume delivered inbox items')

    def test_get_agent_result_consumes_inbox_for_that_agent(self):
        """De-dup: get_agent_result hands back the full answer, so that
        agent's pending <swarm-update> must be dropped — but OTHER agents'
        items stay queued."""
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'x', 'objective': 'X'},
                    {'id': 'y', 'objective': 'Y'},
                ]},
                task={'id': self.task_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 2))
            execute_swarm_tool(
                'get_agent_result',
                {'agent_id': 'x'},
                task={'id': self.task_id},
            )
        # Only y's update should remain.
        remaining = agent_inbox.drain(self.task_id)
        self.assertEqual([it['agent_id'] for it in remaining], ['y'])

    def test_await_timeout_includes_actionable_note(self):
        """On a real timeout, the result must carry an explanatory note
        listing how many finished / are still running so the LLM knows
        what to do next."""
        import threading

        block = threading.Event()

        class _SlowAgent(_FakeAgent):
            def run(self):
                block.wait(timeout=5)
                return self.result

        def _slow_factory(spec, **kwargs):
            return _SlowAgent(spec)

        with patch('lib.swarm.master._build_sub_agent', side_effect=_slow_factory):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'slow', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            raw = execute_swarm_tool(
                'await_agents',
                {'mode': 'all', 'timeout_seconds': 1},
                task={'id': self.task_id},
            )
            block.set()  # release the agent so the driver thread can exit
        result = json.loads(raw)
        self.assertTrue(result['timed_out'])
        self.assertIn('note', result)
        self.assertIn('still running', result['note'])
        self.assertIn('slow', result['still_running'])

    def test_await_when_swarm_finished_returns_note(self):
        """B2: await_agents when nothing is running returns a clear note."""
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            # Wait for the session driver to finish.
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 1,
            ))
            time.sleep(0.1)
            raw = execute_swarm_tool(
                'await_agents',
                {'mode': 'all', 'timeout_seconds': 1},
                task={'id': self.task_id},
            )
        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['timed_out'])
        self.assertIn('note', result)


# ═════════════════════════════════════════════════════════
#  get_agent_result
# ═════════════════════════════════════════════════════════

class TestGetAgentResult(unittest.TestCase):

    def setUp(self):
        self.task_id = 'tgar-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_get_result_returns_full_answer(self):
        with _patch_factory({'a1': {'final_answer': 'X' * 1000}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 1,
            ))

            raw = execute_swarm_tool(
                'get_agent_result',
                {'agent_id': 'a1'},
                task={'id': self.task_id},
            )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'ok')
        self.assertTrue(payload['found'])
        self.assertEqual(payload['agent_id'], 'a1')
        self.assertEqual(len(payload['final_answer']), 1000)

    def test_get_result_unknown_id(self):
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            raw = execute_swarm_tool(
                'get_agent_result',
                {'agent_id': 'nonexistent'},
                task={'id': self.task_id},
            )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'error')
        self.assertFalse(payload['found'])

    def test_get_result_no_session(self):
        raw = execute_swarm_tool(
            'get_agent_result', {'agent_id': 'whatever'},
            task={'id': self.task_id},
        )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'error')

    def test_get_result_falls_back_to_disk_when_session_gone(self):
        """The in-memory session is the fast path; the per-agent .log file
        is the durable fallback. After the session is removed (TTL evict /
        recycle / task end), get_agent_result must still recover the full
        transcript from disk instead of returning 'no active swarm'."""
        import os
        from lib.swarm.integration import _remove_session, _resolve_output_dir
        out_dir = _resolve_output_dir(self.task_id)
        os.makedirs(out_dir, exist_ok=True)
        log_path = os.path.join(out_dir, 'gone1.log')
        with open(log_path, 'w', encoding='utf-8') as fp:
            fp.write('FULL TRANSCRIPT ' * 50)
        try:
            # No session registered for this task at all.
            _remove_session(self.task_id)
            raw = execute_swarm_tool(
                'get_agent_result', {'agent_id': 'gone1'},
                task={'id': self.task_id},
            )
            payload = json.loads(raw)
            self.assertEqual(payload['status'], 'ok')
            self.assertTrue(payload['found'])
            self.assertEqual(payload['source'], 'disk')
            self.assertIn('FULL TRANSCRIPT', payload['final_answer'])
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass

    def test_get_result_cross_task_disk_fallback(self):
        """The agent's .log lives under the task_id of the turn that SPAWNED
        it, but get_agent_result is often called on a LATER turn (fresh
        task_id) in the same conversation. The disk fallback must glob across
        task dirs to find ``<agent_id>.log`` regardless of which task asks.
        Regression for conv mpwjy40j1jjue2 (agent 12c7b388 spawned under task
        0c9045cd, asked for under task 6791bfaa)."""
        import os
        from lib.swarm.integration import _remove_session, _resolve_output_dir
        spawn_task = self.task_id + '-spawned'
        ask_task = self.task_id + '-asked'
        spawn_dir = _resolve_output_dir(spawn_task)
        os.makedirs(spawn_dir, exist_ok=True)
        log_path = os.path.join(spawn_dir, 'xtask1.log')
        with open(log_path, 'w', encoding='utf-8') as fp:
            fp.write('CROSS TASK ANSWER ' * 20)
        try:
            _remove_session(spawn_task)
            _remove_session(ask_task)
            raw = execute_swarm_tool(
                'get_agent_result', {'agent_id': 'xtask1'},
                task={'id': ask_task},  # different task than the one that spawned it
            )
            payload = json.loads(raw)
            self.assertEqual(payload['status'], 'ok')
            self.assertTrue(payload['found'])
            self.assertEqual(payload['source'], 'disk')
            self.assertIn('CROSS TASK ANSWER', payload['final_answer'])
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass


# ═════════════════════════════════════════════════════════
#  Sub-agent denylist (re-asserted at integration layer)
# ═════════════════════════════════════════════════════════

class TestSubAgentDenylist(unittest.TestCase):

    def test_master_orchestrator_strips_denylist_for_general_role(self):
        """The factory used at scheduler runtime must build SubAgents
        whose tool list excludes everything in SUB_AGENT_DENYLIST,
        regardless of role."""
        from lib.swarm.tools import SUB_AGENT_DENYLIST
        all_tools = [
            {'type': 'function',
             'function': {'name': name, 'description': '...', 'parameters': {}}}
            for name in (
                'spawn_agents', 'await_agents', 'get_agent_result',
                'ask_human', 'web_search', 'read_files',
            )
        ]
        # Construct a real SubAgent via the master factory — no LLM call
        # happens during __init__, only during run().
        spec = SubTaskSpec(role='general', objective='hi', id='only')
        orch = MasterOrchestrator(
            task_id='t-denylist', conv_id='c1', specs=[spec],
            all_tools=all_tools,
        )
        agent = orch._make_agent(spec)

        names = {t.get('function', {}).get('name', '')
                 for t in agent.tools}
        for forbidden in SUB_AGENT_DENYLIST:
            self.assertNotIn(forbidden, names,
                              f'{forbidden} leaked into sub-agent {agent.agent_id}')


class TestSpawnEdgeCases(unittest.TestCase):
    """Edge cases for spawn_agents bug fixes (B5, B6)."""

    def setUp(self):
        self.task_id = 'tedge-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_spawn_after_session_terminated_recycles(self):
        """B6: spawn_agents on a task whose previous session terminated
        should recycle and create a fresh one, not return an error."""
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'first', 'objective': 'first wave'}]},
                task={'id': self.task_id},
            )
            # Wait for the session to terminate.
            from lib.swarm.integration import get_active_session
            self.assertTrue(_wait_until(
                lambda: (get_active_session(self.task_id)
                         and get_active_session(self.task_id).is_terminated),
                timeout=3.0,
            ))

            raw = execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'second', 'objective': 'second wave'}]},
                task={'id': self.task_id},
            )
        handle = json.loads(raw)
        self.assertEqual(handle['status'], 'async_launched')
        self.assertEqual(handle['agents'][0]['id'], 'second')
        # is_followup should be False — old session was recycled
        self.assertFalse(handle.get('is_followup'),
                          'recycled session should not be marked as followup')

    def test_spawn_followup_dedup_surfaces_in_handle(self):
        """B5: when add_specs deduplicates an objective, the LLM should
        see the dropped specs in the 'deduplicated' field, not silently
        get a handle with phantom agents."""
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'orig', 'objective': 'investigate Foo bar'}]},
                task={'id': self.task_id},
            )
            # Followup with same objective text — should be deduplicated.
            raw = execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'dup1',  'objective': 'investigate Foo bar'},
                    {'id': 'real2', 'objective': 'investigate Foo bar 2'},
                ]},
                task={'id': self.task_id},
            )
        handle = json.loads(raw)
        self.assertEqual(handle['status'], 'async_launched')
        ids_in_handle = {a['id'] for a in handle['agents']}
        self.assertNotIn('dup1', ids_in_handle, 'duplicate must NOT be in agents')
        self.assertIn('real2', ids_in_handle)
        self.assertIn('deduplicated', handle, 'handle must surface the drop')

    def test_spawn_followup_specs_visible_via_get_status(self):
        """B15: followup specs must appear in get_status() output so
        /api/swarm/status sees the full agent list, not just wave 1."""
        from lib.swarm.integration import get_active_session
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'wave1-a', 'objective': 'first'}]},
                task={'id': self.task_id},
            )
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'wave2-b', 'objective': 'second'}]},
                task={'id': self.task_id},
            )
        sess = get_active_session(self.task_id)
        self.assertIsNotNone(sess)
        status = sess.get_status()
        self.assertIn('wave1-a', status, f'wave1 missing from {list(status)}')
        self.assertIn('wave2-b', status,
                       f'B15: followup spec missing from get_status: {list(status)}')


class TestSessionTTLLiveness(unittest.TestCase):
    """#2: TTL eviction must NOT kill a swarm whose parent task is still
    running. A long task that parked a wave >30 min ago and kept working
    must still be able to await / get_agent_result."""

    def setUp(self):
        self.task_id = 'tttl-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)
        from lib.tasks_pkg.manager import tasks as _chat_tasks
        _chat_tasks.pop(self.task_id, None)

    def _force_stale(self):
        """Push the session's timestamp past the TTL and reset the cleanup
        throttle so the next cleanup pass would evict it."""
        import lib.swarm.integration as integ
        with integ._sessions_lock:
            integ._session_timestamps[self.task_id] = (
                time.time() - integ.SESSION_TTL_SECONDS - 10)
            integ._last_cleanup = 0.0

    def test_live_task_session_survives_ttl(self):
        from lib.swarm.integration import _cleanup_stale_sessions, get_active_session
        from lib.tasks_pkg.manager import tasks as _chat_tasks
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
        _chat_tasks[self.task_id] = {'id': self.task_id, 'status': 'running'}
        self._force_stale()
        with self.subTest('cleanup'):
            import lib.swarm.integration as integ
            with integ._sessions_lock:
                _cleanup_stale_sessions()
        self.assertIsNotNone(get_active_session(self.task_id),
                             'live-task session must survive TTL eviction')

    def test_finished_task_session_evicted_by_ttl(self):
        from lib.swarm.integration import _cleanup_stale_sessions, get_active_session
        from lib.tasks_pkg.manager import tasks as _chat_tasks
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
        _chat_tasks[self.task_id] = {'id': self.task_id, 'status': 'done'}
        self._force_stale()
        import lib.swarm.integration as integ
        with integ._sessions_lock:
            _cleanup_stale_sessions()
        self.assertIsNone(get_active_session(self.task_id),
                          'finished-task session past TTL must be evicted')


class TestSwarmDecoupledFromProject(unittest.TestCase):
    """Swarm tools must be available even without project mode (decoupled
    on 2026-05-28; mirrors read_files decoupling)."""

    def test_bare_conversation_swarm_has_all_three_tools(self):
        from lib.tasks_pkg.model_config import _assemble_tool_list
        tool_list, has_real, _ = _assemble_tool_list(
            cfg={}, project_path='', project_enabled=False,
            task_id='t-bare', search_mode='off', search_enabled=False,
            fetch_enabled=True, code_exec_enabled=False,
            browser_enabled=False, desktop_enabled=False,
            swarm_enabled=True,
            image_gen_enabled=False, human_guidance_enabled=False,
            scheduler_enabled=False, messages=[],
        )
        self.assertIsNotNone(tool_list, 'tool_list should not be None')
        names = {t['function']['name'] for t in tool_list}
        self.assertIn('spawn_agents', names)
        self.assertIn('await_agents', names)
        self.assertIn('get_agent_result', names)

    def test_swarm_off_does_not_inject_tools(self):
        """Negative: swarm tools only show when swarm_enabled is true."""
        from lib.tasks_pkg.model_config import _assemble_tool_list
        tool_list, _, _ = _assemble_tool_list(
            cfg={}, project_path='', project_enabled=True,
            task_id='t-noswarm', search_mode='off', search_enabled=False,
            fetch_enabled=True, code_exec_enabled=False,
            browser_enabled=False, desktop_enabled=False,
            swarm_enabled=False,  # ← OFF
            image_gen_enabled=False, human_guidance_enabled=False,
            scheduler_enabled=False, messages=[],
        )
        names = {t['function']['name'] for t in (tool_list or [])}
        self.assertNotIn('spawn_agents', names)
        self.assertNotIn('await_agents', names)

    def test_swarm_prompt_injected_without_project(self):
        """The <parallel_execution> system prompt must inject when swarm
        is on, regardless of project state."""
        from lib.tasks_pkg.system_context import _inject_system_contexts
        msgs = [{'role': 'system', 'content': 'You are an assistant.'}]
        _inject_system_contexts(
            msgs, project_path='', project_enabled=False,
            memory_enabled=False, search_enabled=False,
            swarm_enabled=True,
            has_real_tools=True, conv_id='c1',
        )
        joined = ' '.join(
            (m.get('content', '') if isinstance(m.get('content'), str)
             else str(m.get('content', '')))
            for m in msgs
        )
        self.assertIn('<parallel_execution>', joined,
                       '<parallel_execution> should inject in bare-mode swarm')


class TestParentConfigPropagation(unittest.TestCase):
    """B14: parent's task['config'] (esp. browserClientId) must propagate
    to sub-agents, otherwise per-client routing breaks silently."""

    def setUp(self):
        self.task_id = 'tcfg-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_browser_client_id_propagates_to_subagent_proxy(self):
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={
                    'id': self.task_id,
                    'config': {'browserClientId': 'client-XYZ'},
                },
            )
        from lib.swarm.integration import get_active_session
        sess = get_active_session(self.task_id)
        self.assertIsNotNone(sess)
        self.assertEqual(
            sess._parent_task_proxy.get('config', {}).get('browserClientId'),
            'client-XYZ',
            'browserClientId did not propagate to sub-agent parent_task_proxy',
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
