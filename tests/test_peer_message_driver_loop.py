"""tests/test_peer_message_driver_loop.py — peer-inbox drain helper contract.

Flow and virtual-user driver loops call ``drain_peer_messages_into`` at their
iteration boundary. The helper lives in ``lib/tasks_pkg/orchestrator/_turn.py``
and shares the main ``run_task`` delivery semantics. This file pins its public
contract directly (unmatched-tool_call defer, coalesced injection + stash,
``_peer_drain_key`` resolution, swarm-item non-drain).

Run::

    python -m pytest tests/test_peer_message_driver_loop.py -v
"""

from __future__ import annotations

import os
import unittest

import pytest

pytestmark = pytest.mark.unit


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
        from lib.tasks_pkg.orchestrator._turn import drain_peer_messages_into
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
        from lib.tasks_pkg.orchestrator._turn import drain_peer_messages_into
        t = self._task()
        agent_inbox.enqueue(t['convId'], '[Peer message] alpha', mode='peer-msg',
                            extra={'queueId': 'qA', 'fromConv': 'c'})
        messages = [{'role': 'user', 'content': 'go'}]
        n = drain_peer_messages_into(t, messages)
        self.assertEqual(n, 1)
        self.assertEqual(messages[-1]['role'], 'user')
        self.assertIn('alpha', messages[-1]['content'])
        self.assertTrue(messages[-1].get('_isInboxInject'))
        self.assertFalse(messages[-1].get('_containsHumanSteer'))
        # Stashed for the deferred chip + durable-row de-dup.
        pending = t.get('_peer_inject_pending') or []
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].get('queueId'), 'qA')

    def test_uses_peer_drain_key_over_convid(self):
        """VU sub-task shape: convId='' but _peer_drain_key=<parent>. The drain
        must read the inbox under _peer_drain_key."""
        from lib import agent_inbox
        from lib.tasks_pkg.orchestrator._turn import drain_peer_messages_into
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
        from lib.tasks_pkg.orchestrator._turn import drain_peer_messages_into
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
