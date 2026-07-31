"""tests/test_peer_message_driver_loop.py — Pillar #6 DRIVER-LOOP fast path.

The main ``run_task`` round loop already drains peer messages at each round
boundary. But the endpoint (Planner→Worker→Critic) and VU sub-task loops are
DRIVER loops that own their own iteration boundary — historically they had NO
inbox drain, so a sibling's peer message stranded in the input-box queue lane
until the WHOLE big task ended (the reported pain: "queuing in the input box …
requires waiting for a large task to complete").

The fix gives those driver loops an iteration-boundary drain hook
(``orchestrator.drain_peer_messages_into``) so a peer message arriving mid-task
is injected as a tool turn on the NEXT iteration — exactly like swarm-update.
This file proves, with the REAL ``run_endpoint_task`` loop (planner/worker/critic
stubbed, no LLM), that:

  1. GREEN — a peer message enqueued after iteration 1 appears in the Worker's
     message list at iteration 2 (delivered mid-task, not at the end).
  2. NEUTER — with the driver hook removed, the same message does NOT appear in
     any iteration's Worker messages: it stays in the queue lane (the pre-fix
     bug). Proves the hook is load-bearing.

Also covers the guards: the unmatched-tool_call defer, and _peer_driver_owned
suppressing a double-drain.

Run::

    python -m pytest tests/test_peer_message_driver_loop.py -v
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest

import pytest

pytestmark = pytest.mark.unit


def _build_task(conv_id):
    return {
        'id': 'drv-' + os.urandom(4).hex(),
        'convId': conv_id,
        'messages': [{'role': 'user', 'content': 'Do the thing.'}],
        'content_lock': threading.Lock(),
        'events': [],
        'events_lock': threading.Lock(),
        'status': 'running',
        'content': '',
        'toolRounds': [],
        'aborted': False,
        'convSettings': {},
        'preset': None,
        'model': 'mock-model',
    }


class EndpointDriverLoopPeerTest(unittest.TestCase):
    """Drive the REAL run_endpoint_task loop, injecting a peer message between
    iterations, and assert the driver-loop drain hook delivers it mid-task."""

    @classmethod
    def setUpClass(cls):
        os.environ['TOFU_ENDPOINT_REPLAN'] = '0'  # keep the loop a plain W→C→W

    def setUp(self):
        from lib import agent_inbox
        agent_inbox.reset_for_test()

    def _run_endpoint_with_peer_at_iter2(self, *, neuter=False):
        """Run run_endpoint_task with 2 worker iterations. A peer inbox item is
        enqueued at the END of iteration 1 (simulating a sibling sending mid-task).
        Returns the list of per-iteration Worker message snapshots.

        When ``neuter`` is True, drain_peer_messages_into is replaced by a no-op
        BEFORE the loop starts — reproducing the pre-fix (no driver hook) state.
        """
        # The driver loop lives in lib/tasks_pkg/endpoint/_run.py — the
        # package facade only re-exports run_endpoint_task, so patching the
        # facade's attributes would never bite the loop's module-level names.
        import lib.tasks_pkg.endpoint._run as ep_mod
        from lib import agent_inbox

        task = _build_task('epconv00000001')
        # The inbox key for a conv-scoped endpoint task is the conv id.
        peer_key = task['convId']

        worker_msg_snapshots = []
        call_log = {'planner': 0, 'worker': 0, 'critic': 0}

        def fake_planner(t, messages, *, planner_tag='initial'):
            call_log['planner'] += 1
            return {'content': '## Goal\nDo it.\n## Checklist\n1. A',
                    'thinking': '', 'usage': {}, 'messages': [], 'error': None}

        def fake_single_turn(t, messages_override=None):
            idx = call_log['worker']
            call_log['worker'] += 1
            # Snapshot the messages the worker sees THIS iteration (this is
            # where a driver-injected peer turn would appear).
            worker_msg_snapshots.append(list(messages_override or []))
            t['toolRounds'] = [{'roundNum': 1, 'toolName': 'apply_diff'}]
            # After the FIRST worker turn, a sibling sends a peer message: its
            # fast-path twin lands in the inbox under the conv key.
            if idx == 0:
                agent_inbox.enqueue(
                    peer_key, '[Peer message] watch the parser epic',
                    priority='next', mode='peer-msg',
                    extra={'queueId': 'qid-driver-1', 'fromConv': 'siblingconv01'})
            return {'content': f'worker {idx}', 'thinking': '',
                    'toolRounds': t['toolRounds'], 'usage': {},
                    'messages': list(messages_override or []) + [
                        {'role': 'assistant', 'content': f'worker {idx}'}],
                    'error': None}

        def fake_critic(t, original_messages, worker_messages, *,
                        iteration=0, latest_tool_rounds=None,
                        cumulative_state_changing=0):
            call_log['critic'] += 1
            # Iteration 1 → CONTINUE_WORKER (loop continues); iteration 2 → STOP.
            if call_log['critic'] == 1:
                return {'feedback': 'keep going', 'next_phase': 'worker',
                        'should_stop': False, 'plan_defect': None,
                        'content': 'keep going\n[VERDICT: CONTINUE_WORKER]',
                        'thinking': '', 'usage': {}, 'error': None}
            return {'feedback': 'done ✅', 'next_phase': 'stop',
                    'should_stop': True, 'plan_defect': None,
                    'content': 'done ✅\n[VERDICT: STOP]',
                    'thinking': '', 'usage': {}, 'error': None}

        # ── Install hermetic stubs ──
        saved = {}
        for name, val in [
            ('_run_planner_turn', fake_planner),
            ('_run_critic_turn', fake_critic),
            ('_run_single_turn', fake_single_turn),
            ('_sync_endpoint_turns_to_conversation', lambda t, turns: 0),
            ('append_event', lambda t, evt: None),
            ('persist_task_result', lambda t: None),
            ('_trigger_per_turn_auto_translate', lambda *a, **k: None),
        ]:
            saved[name] = getattr(ep_mod, name)
            setattr(ep_mod, name, val)

        neuter_saved = None
        if neuter:
            neuter_saved = ep_mod.drain_peer_messages_into
            ep_mod.drain_peer_messages_into = lambda *a, **k: 0

        try:
            ep_mod.run_endpoint_task(task)
        finally:
            for name, orig in saved.items():
                setattr(ep_mod, name, orig)
            if neuter_saved is not None:
                ep_mod.drain_peer_messages_into = neuter_saved

        return worker_msg_snapshots, task

    def _peer_in_snapshot(self, snap):
        return any('watch the parser epic' in (m.get('content') or '')
                   for m in snap if m.get('role') == 'user')

    def test_GREEN_peer_message_injected_at_next_iteration(self):
        """The peer message enqueued after iteration 1 must appear in the
        Worker's messages at iteration 2 — delivered MID-TASK, not at the end."""
        snapshots, task = self._run_endpoint_with_peer_at_iter2()
        self.assertGreaterEqual(len(snapshots), 2,
                                'expected at least 2 worker iterations')
        # Iteration 1 (index 0): peer not sent yet → absent.
        self.assertFalse(self._peer_in_snapshot(snapshots[0]),
                         'peer message must NOT be present before it was sent')
        # Iteration 2 (index 1): the driver hook drained + injected it.
        self.assertTrue(self._peer_in_snapshot(snapshots[1]),
                        'the driver-loop hook must inject the peer message at '
                        'the next iteration boundary (mid-task delivery)')
        # And the endpoint task is flagged as owning peer delivery.
        self.assertTrue(task.get('_peer_driver_owned'),
                        'the endpoint loop must set _peer_driver_owned so the '
                        'nested run_task does not double-drain')

    def test_NEUTER_without_driver_hook_message_strands(self):
        """NEGATIVE CONTROL — remove the driver drain hook. The peer message is
        NEVER injected into any Worker iteration; it stays in the queue lane
        (the pre-fix stranding). Proves the hook is load-bearing."""
        from lib import agent_inbox
        snapshots, task = self._run_endpoint_with_peer_at_iter2(neuter=True)
        for i, snap in enumerate(snapshots):
            self.assertFalse(self._peer_in_snapshot(snap),
                             f'iteration {i}: without the driver hook the peer '
                             f'message must NOT appear (it strands in the queue)')
        # It is still sitting in the inbox, undrained (the queue-lane fallback).
        self.assertEqual(agent_inbox.peek('epconv00000001'), 1,
                         'the undelivered twin remains in the inbox = the '
                         'stranding the driver hook fixes')


class DrainHelperGuardTest(unittest.TestCase):
    """Direct unit tests of drain_peer_messages_into's contract."""

    def setUp(self):
        from lib import agent_inbox
        agent_inbox.reset_for_test()

    def _task(self, conv='guardconv00001'):
        return {'id': 'g-' + os.urandom(3).hex(), 'convId': conv}

    def test_defers_on_unmatched_tool_call(self):
        """A trailing assistant tool_call awaiting its result must DEFER the
        inject (never split a tool_call/tool_result pair)."""
        from lib import agent_inbox
        from lib.tasks_pkg.orchestrator import drain_peer_messages_into
        t = self._task()
        agent_inbox.enqueue(t['convId'], '[Peer message] hi', mode='peer-msg',
                            extra={'queueId': 'q1', 'fromConv': 'c'})
        messages = [{'role': 'assistant', 'content': '',
                     'tool_calls': [{'id': 'tc1', 'function': {'name': 'x'}}]}]
        n = drain_peer_messages_into(t, messages)
        self.assertEqual(n, 0, 'must defer while a tool_call is unmatched')
        # Message untouched, item still queued for the next boundary.
        self.assertEqual(len(messages), 1)
        self.assertEqual(agent_inbox.peek(t['convId']), 1)

    def test_injects_and_stashes_for_deferred_flush(self):
        """A clean boundary injects one coalesced user message AND stashes the
        items under _peer_inject_pending for the run_task flush."""
        from lib import agent_inbox
        from lib.tasks_pkg.orchestrator import drain_peer_messages_into
        t = self._task()
        agent_inbox.enqueue(t['convId'], '[Peer message] alpha', mode='peer-msg',
                            extra={'queueId': 'qA', 'fromConv': 'c'})
        messages = [{'role': 'user', 'content': 'go'}]
        n = drain_peer_messages_into(t, messages)
        self.assertEqual(n, 1)
        self.assertEqual(messages[-1]['role'], 'user')
        self.assertIn('alpha', messages[-1]['content'])
        # Stashed for the deferred chip + durable-row de-dup.
        pending = t.get('_peer_inject_pending') or []
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].get('queueId'), 'qA')

    def test_uses_peer_drain_key_over_convid(self):
        """VU sub-task shape: convId='' but _peer_drain_key=<parent>. The drain
        must read the inbox under _peer_drain_key."""
        from lib import agent_inbox
        from lib.tasks_pkg.orchestrator import drain_peer_messages_into
        t = {'id': 'vu-1', 'convId': '', '_peer_drain_key': 'parentconv0001'}
        agent_inbox.enqueue('parentconv0001', '[Peer message] beta',
                            mode='peer-msg',
                            extra={'queueId': 'qB', 'fromConv': 'c'})
        messages = [{'role': 'user', 'content': 'go'}]
        n = drain_peer_messages_into(t, messages)
        self.assertEqual(n, 1, 'must drain under _peer_drain_key (VU sub-task)')
        self.assertIn('beta', messages[-1]['content'])

    def test_leaves_swarm_items_untouched(self):
        """The driver hook drains ONLY peer-msg items; a swarm-update item in
        the same inbox is left for the main loop."""
        from lib import agent_inbox
        from lib.tasks_pkg.orchestrator import drain_peer_messages_into
        t = self._task()
        agent_inbox.enqueue(t['convId'], '<swarm-update>x</swarm-update>',
                            mode='swarm-update', agent_id='a1')
        agent_inbox.enqueue(t['convId'], '[Peer message] gamma', mode='peer-msg',
                            extra={'queueId': 'qG', 'fromConv': 'c'})
        messages = [{'role': 'user', 'content': 'go'}]
        n = drain_peer_messages_into(t, messages)
        self.assertEqual(n, 1)
        self.assertIn('gamma', messages[-1]['content'])
        # The swarm item survives (not drained by the peer hook).
        remaining = agent_inbox.drain(t['convId'])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['mode'], 'swarm-update')


if __name__ == '__main__':
    unittest.main()
