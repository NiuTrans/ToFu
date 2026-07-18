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

    def test_await_no_ids_includes_early_finishers(self):
        """Regression: await_agents() with NO ids must report agents that
        finished BEFORE the call. Previously the no-ids branch built its
        wait-set from only running/pending agents and force-emptied
        already_done, so an early finisher vanished — mode='all' returned
        k/N < total while the swarm panel showed N/N green."""
        import threading
        release = threading.Event()

        class _MixedAgent(_FakeAgent):
            def run(self):
                # 'fast' returns at once; 'slow' waits for release.
                if self.spec.id == 'slow':
                    release.wait(timeout=5)
                return self.result

        with patch('lib.swarm.master._build_sub_agent',
                   side_effect=lambda spec, **kw: _MixedAgent(spec)):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'fast', 'objective': 'F'},
                    {'id': 'slow', 'objective': 'S'},
                ]},
                task={'id': self.task_id},
            )
            # Wait until 'fast' has completed (its card is green) while
            # 'slow' is still blocked.
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 1))
            release.set()  # now let 'slow' finish too
            raw = execute_swarm_tool(
                'await_agents',
                {'mode': 'all', 'timeout_seconds': 5},
                task={'id': self.task_id},
            )
        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['timed_out'])
        ids_seen = {p['agent_id'] for p in result['completed']}
        self.assertEqual(ids_seen, {'fast', 'slow'},
                         'no-ids await must include the early finisher')

    def test_await_no_ids_when_all_already_done(self):
        """Regression: await_agents() with no ids, called after ALL agents
        already finished, must return them (not an empty 0/N 'finished'
        note)."""
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'p', 'objective': 'P'},
                    {'id': 'q', 'objective': 'Q'},
                ]},
                task={'id': self.task_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 2))
            time.sleep(0.1)  # let the driver thread terminate
            raw = execute_swarm_tool(
                'await_agents',
                {'mode': 'all', 'timeout_seconds': 5},
                task={'id': self.task_id},
            )
        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['timed_out'])
        self.assertEqual({p['agent_id'] for p in result['completed']},
                         {'p', 'q'},
                         'all-done no-ids await must return every agent')

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

    def test_await_breaks_when_swarm_terminated_with_stranded_agent(self):
        """Root cause of 'panel shows all done but the await tool keeps
        spinning to the hard-cap timeout': once the driver thread has
        exited (``_terminated``), an agent still in ``to_wait`` that never
        landed in ``_results_by_id`` (e.g. a cancel_pending'd / dropped
        spec) can NEVER complete — yet the panel's swarm_phase:complete
        sweep already marked every card done. The wait loop must break on
        ``_terminated`` and return PROMPTLY (not timed_out), reporting the
        stranded id as still_running, instead of blocking for the full
        window."""
        spec_done = SubTaskSpec(role='general', objective='done one', id='ok')
        spec_lost = SubTaskSpec(role='general', objective='lost one', id='lost')
        orch = MasterOrchestrator(
            task_id=self.task_id, conv_id='c1',
            specs=[spec_done, spec_lost],
        )
        # Build the scheduler WITHOUT running the driver, so we can model the
        # exact desync state deterministically.
        orch._scheduler = orch._build_scheduler()
        # 'ok' completed and is recorded; 'lost' is still 'running' from the
        # await snapshot's POV but will never produce a result.
        result_ok = SubAgentResult(
            status=SubAgentStatus.COMPLETED.value,
            final_answer='ok answer', elapsed_seconds=0.1,
            total_tokens=10, rounds_used=1)
        with orch._lock:
            orch._results_by_id['ok'] = (spec_done, result_ok)
        orch._scheduler._running['lost'] = spec_lost
        # The driver has exited (scheduler drained / aborted) — terminal.
        orch._terminated = True

        t0 = time.time()
        result = orch.await_agents(mode='all', ids=None, timeout_seconds=10)
        elapsed = time.time() - t0

        self.assertLess(elapsed, 2.0,
                        f'must break on _terminated, not block; took {elapsed:.2f}s')
        self.assertFalse(result['timed_out'],
                         'a terminated swarm is not a wall-clock timeout')
        completed_ids = {p['agent_id'] for p in result['completed']}
        self.assertIn('ok', completed_ids)
        self.assertIn('lost', result['still_running'],
                      'stranded agent reported as still_running, not awaited')
        self.assertIn('note', result)
        self.assertIn('lost', result['note'])

    def test_await_terminated_but_all_done_is_clean(self):
        """Guard: the _terminated break must NOT manufacture a spurious
        note/timeout when every requested agent actually completed before
        the driver exited (the normal happy path)."""
        spec_a = SubTaskSpec(role='general', objective='A', id='a')
        orch = MasterOrchestrator(
            task_id=self.task_id, conv_id='c1', specs=[spec_a])
        orch._scheduler = orch._build_scheduler()
        res = SubAgentResult(
            status=SubAgentStatus.COMPLETED.value, final_answer='x',
            elapsed_seconds=0.1, total_tokens=5, rounds_used=1)
        with orch._lock:
            orch._results_by_id['a'] = (spec_a, res)
        orch._terminated = True  # driver already exited, nothing stranded

        result = orch.await_agents(mode='all', ids=None, timeout_seconds=10)
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['still_running'], [])
        self.assertEqual({p['agent_id'] for p in result['completed']}, {'a'})


# ═════════════════════════════════════════════════════════
#  get_agent_result
# ═════════════════════════════════════════════════════════


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

    def test_get_result_batch_returns_all(self):
        """Batch mode: ``agent_ids`` fetches several agents' full bodies in a
        single call, returned together under ``results`` (mirrors the
        read_files/web_search/fetch_url batch contract)."""
        with _patch_factory({
            'a1': {'final_answer': 'X' * 100},
            'a2': {'final_answer': 'Y' * 200},
            'a3': {'final_answer': 'Z' * 300},
        }):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [
                    {'id': 'a1', 'objective': 'O1'},
                    {'id': 'a2', 'objective': 'O2'},
                    {'id': 'a3', 'objective': 'O3'},
                ]},
                task={'id': self.task_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 3,
            ))
            raw = execute_swarm_tool(
                'get_agent_result',
                {'agent_ids': ['a1', 'a2', 'a3']},
                task={'id': self.task_id},
            )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'ok')
        results = payload['results']
        self.assertEqual(len(results), 3)
        by_id = {r['agent_id']: r for r in results}
        self.assertEqual(len(by_id['a1']['final_answer']), 100)
        self.assertEqual(len(by_id['a2']['final_answer']), 200)
        self.assertEqual(len(by_id['a3']['final_answer']), 300)
        self.assertTrue(all(r['found'] for r in results))

    def test_get_result_batch_mixed_known_unknown(self):
        """A batch with an unknown id still returns the known agent's body —
        the bad id gets its own per-entry error and never aborts the batch."""
        with _patch_factory({'a1': {'final_answer': 'GOOD'}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 1))
            raw = execute_swarm_tool(
                'get_agent_result',
                {'agent_ids': ['a1', 'nope']},
                task={'id': self.task_id},
            )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'ok')
        results = payload['results']
        self.assertEqual(len(results), 2)
        by_id = {r['agent_id']: r for r in results}
        self.assertTrue(by_id['a1']['found'])
        self.assertIn('GOOD', by_id['a1']['final_answer'])
        self.assertFalse(by_id['nope']['found'])

    def test_get_result_single_still_works(self):
        """Single-mode ``agent_id`` remains supported (no `results` wrapper)."""
        with _patch_factory({'a1': {'final_answer': 'SOLO'}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.task_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.task_id) >= 1))
            raw = execute_swarm_tool(
                'get_agent_result', {'agent_id': 'a1'},
                task={'id': self.task_id},
            )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'ok')
        self.assertNotIn('results', payload)
        self.assertEqual(payload['agent_id'], 'a1')
        self.assertIn('SOLO', payload['final_answer'])

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


class TestConvScopedSession(unittest.TestCase):
    """Option A: a swarm spawned on one turn is reachable from LATER turns
    in the SAME conversation (each turn has a fresh task_id but shares the
    conv id). This is the fix for the 'continue → await → no active swarm'
    bug where a session was torn down at the spawning turn's end."""

    def setUp(self):
        self.conv_id = 'cscope-' + str(id(self))
        self.spawn_task = self.conv_id + '-t1'
        self.cont_task = self.conv_id + '-t2'
        for k in (self.conv_id, self.spawn_task, self.cont_task):
            _reset_global_state(k)

    def tearDown(self):
        import lib.swarm.integration as integ
        with integ._sessions_lock:
            integ._active_sessions.pop(self.conv_id, None)
            for a in (self.spawn_task, self.cont_task):
                integ._key_aliases.pop(a, None)
        for k in (self.conv_id, self.spawn_task, self.cont_task):
            _reset_global_state(k)

    def test_session_keyed_by_conv_id(self):
        """The session is registered under the conv id, not the task id."""
        from lib.swarm.integration import get_active_session
        with _patch_factory():
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.spawn_task, 'convId': self.conv_id},
            )
        self.assertIsNotNone(get_active_session(self.conv_id),
                             'session must be reachable by conv id')
        # And reachable via the spawning task id (alias).
        self.assertIsNotNone(get_active_session(self.spawn_task),
                             'session must be reachable by spawning task id alias')

    def test_await_resolves_from_later_turn_same_conv(self):
        """The crux: a 'continue' turn (fresh task_id, same conv) must reach
        the live swarm and get results — NOT 'no active swarm session'."""
        with _patch_factory({'a1': {'final_answer': 'done already'}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.spawn_task, 'convId': self.conv_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.conv_id) >= 1))
            # New turn: different task id, same conv.
            raw = execute_swarm_tool(
                'await_agents',
                {'ids': ['a1'], 'mode': 'all', 'timeout_seconds': 5},
                task={'id': self.cont_task, 'convId': self.conv_id},
            )
        result = json.loads(raw)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual({p['agent_id'] for p in result['completed']}, {'a1'})

    def test_get_result_resolves_from_later_turn_same_conv(self):
        with _patch_factory({'a1': {'final_answer': 'Z' * 500}}):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'a1', 'objective': 'O'}]},
                task={'id': self.spawn_task, 'convId': self.conv_id},
            )
            self.assertTrue(_wait_until(
                lambda: agent_inbox.peek(self.conv_id) >= 1))
            raw = execute_swarm_tool(
                'get_agent_result', {'agent_id': 'a1'},
                task={'id': self.cont_task, 'convId': self.conv_id},
            )
        payload = json.loads(raw)
        self.assertEqual(payload['status'], 'ok')
        self.assertTrue(payload['found'])
        self.assertEqual(len(payload['final_answer']), 500)


class TestTeardownDecoupledFromTurn(unittest.TestCase):
    """Option A: ``run_task`` finalization must NOT abort a still-running
    swarm on a normal turn end — only on explicit user abort. Verified at
    the integration level by simulating the orchestrator teardown branch."""

    def setUp(self):
        self.conv_id = 'tteardown-' + str(id(self))
        self.task_id = self.conv_id + '-t1'
        for k in (self.conv_id, self.task_id):
            _reset_global_state(k)

    def tearDown(self):
        import lib.swarm.integration as integ
        with integ._sessions_lock:
            integ._active_sessions.pop(self.conv_id, None)
            integ._key_aliases.pop(self.task_id, None)
        for k in (self.conv_id, self.task_id):
            _reset_global_state(k)

    def _spawn_live(self, block):
        """Spawn a swarm whose single agent blocks until *block* is set, so
        the session stays live (is_terminated False) during the test."""
        class _SlowAgent(_FakeAgent):
            def run(self):
                block.wait(timeout=5)
                return self.result

        return patch('lib.swarm.master._build_sub_agent',
                     side_effect=lambda spec, **kw: _SlowAgent(spec))

    def test_normal_turn_end_detaches_not_aborts(self):
        """Replicates the orchestrator teardown branch with aborted=False:
        a live swarm must survive (still registered, not aborted)."""
        import threading
        from lib.swarm.integration import (get_active_session, swarm_key_for,
                                            _remove_session)
        block = threading.Event()
        with self._spawn_live(block):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'slow', 'objective': 'O'}]},
                task={'id': self.task_id, 'convId': self.conv_id},
            )
            # Simulate orchestrator finalization on a NORMAL turn end.
            task = {'id': self.task_id, 'convId': self.conv_id, 'aborted': False}
            key = swarm_key_for(task)
            sess = get_active_session(key)
            self.assertIsNotNone(sess)
            user_aborted = bool(task.get('aborted'))
            if sess is not None and (user_aborted or sess.is_terminated):
                sess.abort(); _remove_session(key)
            # Normal end → DETACH, do nothing.
            self.assertIsNotNone(get_active_session(self.conv_id),
                                 'normal turn end must NOT remove a live swarm')
            self.assertFalse(sess._aborted,
                             'normal turn end must NOT abort a live swarm')
            block.set()

    def test_user_abort_tears_down(self):
        """With aborted=True the teardown branch DOES abort + remove."""
        import threading
        from lib.swarm.integration import (get_active_session, swarm_key_for,
                                            _remove_session)
        from lib.agent_inbox import clear as _clear_inbox
        block = threading.Event()
        with self._spawn_live(block):
            execute_swarm_tool(
                'spawn_agents',
                {'agents': [{'id': 'slow', 'objective': 'O'}]},
                task={'id': self.task_id, 'convId': self.conv_id},
            )
            task = {'id': self.task_id, 'convId': self.conv_id, 'aborted': True}
            key = swarm_key_for(task)
            sess = get_active_session(key)
            self.assertIsNotNone(sess)
            user_aborted = bool(task.get('aborted'))
            if sess is not None and (user_aborted or sess.is_terminated):
                sess.abort(); _remove_session(key); _clear_inbox(key)
            block.set()
        self.assertIsNone(get_active_session(self.conv_id),
                          'user abort must remove the swarm session')
        self.assertTrue(sess._aborted, 'user abort must signal abort')


class TestAwaitDiskFallback(unittest.TestCase):
    """Option A: await_agents with explicit ids and NO live session must
    recover results from the durable on-disk transcripts instead of a hard
    'no active swarm' error."""

    def setUp(self):
        self.task_id = 'tawaitdisk-' + str(id(self))
        _reset_global_state(self.task_id)

    def tearDown(self):
        _reset_global_state(self.task_id)

    def test_await_ids_fall_back_to_disk(self):
        import os
        from lib.swarm.integration import _remove_session, _resolve_output_dir
        out_dir = _resolve_output_dir(self.task_id)
        os.makedirs(out_dir, exist_ok=True)
        log_path = os.path.join(out_dir, 'gonea.log')
        with open(log_path, 'w', encoding='utf-8') as fp:
            fp.write('DISK AWAIT ANSWER ' * 10)
        try:
            _remove_session(self.task_id)
            raw = execute_swarm_tool(
                'await_agents',
                {'ids': ['gonea'], 'mode': 'all', 'timeout_seconds': 1},
                task={'id': self.task_id},
            )
            result = json.loads(raw)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result.get('source'), 'disk')
            ids = {c['agent_id'] for c in result['completed']}
            self.assertIn('gonea', ids)
            self.assertIn('DISK AWAIT ANSWER',
                          result['completed'][0]['final_answer'])
        finally:
            try:
                os.remove(log_path)
            except OSError:
                pass

    def test_await_no_ids_no_session_still_errors(self):
        """Without ids there's nothing to scope to on disk → keep the
        existing 'no active swarm' error."""
        from lib.swarm.integration import _remove_session
        _remove_session(self.task_id)
        raw = execute_swarm_tool(
            'await_agents', {'mode': 'all', 'timeout_seconds': 1},
            task={'id': self.task_id},
        )
        self.assertEqual(json.loads(raw)['status'], 'error')


# ═════════════════════════════════════════════════════════
#  Phase 2 — auto-continue guardrails (_maybe_autocontinue)
# ═════════════════════════════════════════════════════════

class TestAutoContinueGuardrails(unittest.TestCase):
    """The settle hook must wake the main agent only under safe conditions:
    enabled, conversation idle, inbox non-empty, latch free, chain < ceiling.
    We patch the actual turn-start so no DB/LLM is touched and count calls."""

    def setUp(self):
        self.key = 'tac-' + str(id(self))
        agent_inbox.reset_for_test(self.key)
        import lib.swarm.integration as integ
        self.integ = integ
        with integ._autocontinue_lock:
            integ._autocontinue_chain.pop(self.key, None)
            integ._autocontinue_inflight.discard(self.key)

    def tearDown(self):
        agent_inbox.reset_for_test(self.key)
        with self.integ._autocontinue_lock:
            self.integ._autocontinue_chain.pop(self.key, None)
            self.integ._autocontinue_inflight.discard(self.key)

    def _enqueue(self):
        agent_inbox.enqueue(self.key, '<swarm-update>x</swarm-update>',
                            priority='later', mode='swarm-update', agent_id='a1')

    def test_fires_when_idle_with_pending(self):
        self._enqueue()
        with patch.object(self.integ, '_key_is_live', return_value=False), \
             patch.object(self.integ, '_start_autocontinue_turn',
                          return_value=True) as start:
            self.integ._maybe_autocontinue(self.key)
        start.assert_called_once_with(self.key)
        self.assertEqual(self.integ._autocontinue_chain.get(self.key), 1)

    def test_skips_when_conversation_live(self):
        self._enqueue()
        with patch.object(self.integ, '_key_is_live', return_value=True), \
             patch.object(self.integ, '_start_autocontinue_turn',
                          return_value=True) as start:
            self.integ._maybe_autocontinue(self.key)
        start.assert_not_called()

    def test_skips_when_inbox_empty(self):
        with patch.object(self.integ, '_key_is_live', return_value=False), \
             patch.object(self.integ, '_start_autocontinue_turn',
                          return_value=True) as start:
            self.integ._maybe_autocontinue(self.key)
        start.assert_not_called()

    def test_chain_ceiling_blocks_runaway(self):
        self._enqueue()
        with self.integ._autocontinue_lock:
            self.integ._autocontinue_chain[self.key] = self.integ.SWARM_AUTOCONTINUE_MAX_CHAIN
        with patch.object(self.integ, '_key_is_live', return_value=False), \
             patch.object(self.integ, '_start_autocontinue_turn',
                          return_value=True) as start:
            self.integ._maybe_autocontinue(self.key)
        start.assert_not_called()

    def test_failed_start_releases_chain_increment(self):
        self._enqueue()
        with patch.object(self.integ, '_key_is_live', return_value=False), \
             patch.object(self.integ, '_start_autocontinue_turn',
                          return_value=False):
            self.integ._maybe_autocontinue(self.key)
        # increment was rolled back so a later settle can retry
        self.assertEqual(self.integ._autocontinue_chain.get(self.key, 0), 0)

    def test_disabled_flag_is_a_noop(self):
        self._enqueue()
        with patch.object(self.integ, 'SWARM_AUTOCONTINUE_ENABLED', False), \
             patch.object(self.integ, '_start_autocontinue_turn',
                          return_value=True) as start:
            self.integ._maybe_autocontinue(self.key)
        start.assert_not_called()

    def test_reset_chain_clears_counter(self):
        with self.integ._autocontinue_lock:
            self.integ._autocontinue_chain[self.key] = 2
        self.integ.reset_autocontinue_chain(self.key)
        self.assertNotIn(self.key, self.integ._autocontinue_chain)


class TestRehydration(unittest.TestCase):
    """Durable round-level resume: rehydrate a swarm from persisted checkpoints.

    Uses the in-memory DB so persistence tables exist. Verifies:
      * completed agents are preloaded into the results map (NOT re-run),
      * a completed-but-undelivered result is re-enqueued as <swarm-update>,
      * non-terminal agents are re-spawned and seeded from their checkpoint,
      * load_resumable_sessions drops fully-terminal+delivered sessions.
    """

    @classmethod
    def setUpClass(cls):
        import os
        os.environ['TOFU_DB_BACKEND'] = 'sqlite'
        os.environ.setdefault('TOFU_DB_PATH', '/tmp/swarm_rehydrate_unittest.db')
        from lib.database import init_db
        init_db()

    def setUp(self):
        from lib.swarm import persistence as p
        self.p = p
        self.key = 'conv-rehy-' + str(id(self))
        agent_inbox.reset_for_test(self.key)

    def tearDown(self):
        self.p.delete_session(self.key)
        with _sessions_lock:
            _active_sessions.pop(self.key, None)
        agent_inbox.reset_for_test(self.key)

    def test_load_resumable_filters_terminal_delivered(self):
        self.p.save_session(self.key, conv_id=self.key, task_id='t1',
                            specs=[{'id': 'a1', 'role': 'researcher', 'objective': 'X'}],
                            config={}, status='running')
        # one completed+delivered agent → NOT resumable
        self.p.save_agent(self.key, 'a1', role='researcher', objective='X',
                          status='completed', messages=[], result={'status': 'completed'},
                          rounds_used=1, delivered=True)
        self.assertEqual(
            [s for s in self.p.load_resumable_sessions() if s['swarm_key'] == self.key],
            [])
        # flip to undelivered → resumable (notification owed)
        self.p.mark_delivered  # noqa  (bare attribute reference, intentional)
        self.p.save_agent(self.key, 'a1', role='researcher', objective='X',
                          status='completed', messages=[], result={'status': 'completed'},
                          rounds_used=1, delivered=False)
        keys = [s['swarm_key'] for s in self.p.load_resumable_sessions()]
        self.assertIn(self.key, keys)

    def test_rehydrate_preloads_completed_and_respawns_running(self):
        # Persist a session: a1 completed (delivered), a2 running mid-flight.
        self.p.save_session(self.key, conv_id=self.key, task_id='t2',
                            specs=[
                                {'id': 'a1', 'role': 'researcher', 'objective': 'done one'},
                                {'id': 'a2', 'role': 'coder', 'objective': 'resume me'},
                            ],
                            config={}, status='running')
        self.p.save_agent(self.key, 'a1', role='researcher', objective='done one',
                          status='completed',
                          messages=[{'role': 'assistant', 'content': 'A1 FINAL'}],
                          result={'status': 'completed', 'final_answer': 'A1 FINAL'},
                          rounds_used=2, delivered=True)
        self.p.save_agent(self.key, 'a2', role='coder', objective='resume me',
                          status='running',
                          messages=[{'role': 'user', 'content': 'go'},
                                    {'role': 'assistant', 'content': 'mid'}],
                          rounds_used=1, delivered=False)

        sess = next(s for s in self.p.load_resumable_sessions()
                    if s['swarm_key'] == self.key)

        specs = [SubTaskSpec.from_dict(d) for d in sess['specs']]
        master = MasterOrchestrator(task_id='t2', conv_id=self.key, specs=specs,
                                    inbox_key=self.key)

        seeded = {}

        def _factory(spec, **kwargs):
            # The factory in the real code seeds agent.messages from
            # _resume_messages AFTER build; here we just record what got passed
            # and return a fast fake. We read master._resume_messages to assert
            # the seed map was populated for the running agent.
            return _FakeAgent(spec, final_answer=f'resumed {spec.id}')

        with patch('lib.swarm.master._build_sub_agent', side_effect=_factory):
            master.rehydrate_in_background(sess['agents'])
            done = _wait_until(lambda: master.completed_count >= 2, timeout=3.0)

        self.assertTrue(done, 'rehydrated swarm did not settle')
        # a1 preloaded with its stored answer (not re-run → answer unchanged)
        r1 = master.get_agent_result('a1')
        self.assertEqual(r1['final_answer'], 'A1 FINAL')
        # a2 was re-spawned: its resume messages were seeded into the map
        self.assertIn('a2', master._resume_messages)
        self.assertEqual(len(master._resume_messages['a2']), 2)

    def test_rehydrate_reenqueues_undelivered_completed(self):
        self.p.save_session(self.key, conv_id=self.key, task_id='t3',
                            specs=[{'id': 'a1', 'role': 'researcher', 'objective': 'X'}],
                            config={}, status='running')
        self.p.save_agent(self.key, 'a1', role='researcher', objective='X',
                          status='completed',
                          messages=[{'role': 'assistant', 'content': 'undelivered ans'}],
                          result={'status': 'completed', 'final_answer': 'undelivered ans'},
                          rounds_used=1, delivered=False)
        sess = next(s for s in self.p.load_resumable_sessions()
                    if s['swarm_key'] == self.key)
        specs = [SubTaskSpec.from_dict(d) for d in sess['specs']]
        master = MasterOrchestrator(task_id='t3', conv_id=self.key, specs=specs,
                                    inbox_key=self.key)
        with _patch_factory():
            master.rehydrate_in_background(sess['agents'])
            _wait_until(lambda: master.is_terminated, timeout=3.0)
        # The undelivered completed result was re-pushed to the model inbox.
        self.assertGreaterEqual(agent_inbox.peek(self.key), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
