"""tests/test_orchestration_vu_mislabel.py — VU turns must not be mislabeled.

Root-cause regression guard for the "编排里的自动驾驶比模式开关更蠢" bug:
when autopilot runs through the FlowExecutor engine (the "编排流程" dropdown),
the ``EndpointEventAdapter`` used to stamp EVERY user-side turn with
``_isEndpointReview`` — including the virtual_user (VU) turns. That marker
makes ``_transform_messages`` (the LLM context builder) SKIP the row
(``_transform.py``: ``if msg.get('_isEndpointReview'): continue``). So the
VU's instruction ("stop analyzing and execute — here is the checklist")
was SILENTLY DROPPED from the model's context, starving the next worker
turn. This is a correctness bug, not a cosmetic label.

These tests assert the fix along the whole marker chain:
  1. adapter stamps ``_isVirtualUser`` (not ``_isEndpointReview``) for a
     virtual_user node, and NEVER stamps a VU row with an endpoint marker;
  2. such a VU row SURVIVES ``_transform_messages`` (reaches context);
  3. a critic flow (control) still stamps ``_isEndpointReview`` and that row
     is still correctly SKIPPED by ``_transform_messages``.
"""

import unittest

import pytest

from lib.orchestration import (
    build_autopilot_definition, build_endpoint_definition,
)
from lib.orchestration_engine import FlowExecutor
from lib.orchestration_endpoint_adapter import EndpointEventAdapter
from lib.tasks_pkg.conv_message_builder._transform import _transform_messages


def _run(defn, runner):
    adapter = EndpointEventAdapter()
    FlowExecutor(defn, agent_runner=runner, on_event=adapter.on_event).run()
    return adapter.messages


def _autopilot_runner():
    """worker (writes) → VU (keep going once, then TASK_DONE)."""
    seq = {'vu': 0}

    def runner(node, ctx, it):
        role = node.get('role')
        if role == 'worker':
            return {'output': 'did the edit', 'status': 'completed',
                    'error': '', 'tool_names': ['write_file']}
        if role == 'virtual_user':
            seq['vu'] += 1
            if seq['vu'] < 2:
                return {'output': 'Stop analyzing and execute — here is the '
                                  'checklist.', 'status': 'completed', 'error': ''}
            return {'output': 'Looks complete. [VU: TASK_DONE]',
                    'status': 'completed', 'error': ''}
        return {'output': 'x', 'status': 'completed', 'error': ''}

    return runner


pytestmark = pytest.mark.unit


class VuMarkerTest(unittest.TestCase):
    def test_vu_turn_marked_virtual_user_not_endpoint_review(self):
        msgs = _run(build_autopilot_definition(max_iterations=4),
                    _autopilot_runner())
        vu_rows = [m for m in msgs
                   if m.get('role') == 'user' and m.get('content')]
        self.assertTrue(vu_rows, 'expected at least one VU user-side turn')
        for m in vu_rows:
            self.assertTrue(m.get('_isVirtualUser'),
                            'VU turn must carry _isVirtualUser')
            self.assertFalse(m.get('_isEndpointReview'),
                             'VU turn must NOT carry _isEndpointReview '
                             '(that marker makes _transform skip it)')
            # It must carry NONE of the three context-skip markers.
            self.assertFalse(m.get('_epIteration'))
            self.assertFalse(m.get('_isEndpointPlanner'))
            # Parity with the live autopilot path (autopilot.py:1081): a VU
            # row carries a routable _msgId.
            self.assertTrue(m.get('_msgId'), 'VU turn should carry a _msgId')

    def test_vu_instruction_survives_context_rebuild(self):
        """The reported correctness bug: the VU instruction must reach the
        model. Rebuild context from a conversation containing a VU turn and
        assert its text is present (NOT skipped)."""
        vu_text = 'Stop analyzing and execute — here is the checklist.'
        raw = [
            {'role': 'user', 'content': 'Add kimi-k3 to the templates.'},
            {'role': 'assistant', 'content': 'analysis only', '_epIteration': 1},
            {'role': 'user', 'content': vu_text, '_isVirtualUser': True,
             '_msgId': 'vu-1'},
        ]
        out = _transform_messages(raw, {})
        user_texts = [m.get('content') for m in out if m.get('role') == 'user']
        self.assertIn(vu_text, user_texts,
                      'VU instruction was dropped from LLM context')

    def test_critic_control_still_marked_and_still_skipped(self):
        # Adapter still stamps critic reviews _isEndpointReview.
        seq = {'w': 0}

        def runner(node, ctx, it):
            role = node.get('role')
            if role == 'worker':
                seq['w'] += 1
                return {'output': f'w{seq["w"]}', 'status': 'completed',
                        'error': '', 'tool_names': ['write_file']}
            if role == 'critic':
                return {'output': '[VERDICT: STOP]', 'status': 'completed',
                        'error': ''}
            return {'output': 'PLAN', 'status': 'completed', 'error': ''}

        msgs = _run(build_endpoint_definition(max_iterations=3), runner)
        critics = [m for m in msgs if m.get('role') == 'user']
        self.assertTrue(critics)
        for m in critics:
            self.assertTrue(m.get('_isEndpointReview'))
            self.assertFalse(m.get('_isVirtualUser'))

        # And a critic row is STILL skipped by the context builder (its
        # feedback is injected via a different mechanism in endpoint mode).
        raw = [
            {'role': 'user', 'content': 'do the work'},
            {'role': 'assistant', 'content': 'worked', '_epIteration': 1},
            {'role': 'user', 'content': 'CRITIC FEEDBACK TEXT',
             '_isEndpointReview': True, '_epIteration': 1},
        ]
        out = _transform_messages(raw, {})
        user_texts = [m.get('content') for m in out if m.get('role') == 'user']
        self.assertNotIn('CRITIC FEEDBACK TEXT', user_texts,
                         'critic review must remain skipped from context')


class VuConsumerTest(unittest.TestCase):
    """The marker change must be honored by the DOWNSTREAM consumers too,
    not just the producer + context builder."""

    def test_sync_boundary_treats_vu_as_engine_turn(self):
        """``_sync_endpoint_turns_to_conversation``'s base/endpoint boundary
        scan must count a marker-less VU row as an ENGINE turn, else it lands
        the boundary after the VU row and re-appends it on every incremental
        sync (duplicated VU turns)."""
        from lib.tasks_pkg.endpoint._translate import (
            _sync_endpoint_turns_to_conversation,
        )
        import lib.tasks_pkg.endpoint._translate as tr

        # A conversation already carrying one endpoint run (worker + VU).
        persisted = [
            {'role': 'user', 'content': 'the ask'},
            {'role': 'assistant', 'content': 'w1', '_epIteration': 1},
            {'role': 'user', 'content': 'keep going', '_isVirtualUser': True,
             '_msgId': 'vu-1'},
        ]
        captured = {}

        class _Store:
            def load_conversation_messages(self, cid):
                return list(persisted), 0, 0

            def sync_conversation_with_search(self, cid, msgs, *,
                                              expected_rev=None, rebuild=None):
                captured['msgs'] = msgs

        import types
        fake_mod = types.SimpleNamespace(get_conversation_store=lambda: _Store())
        import sys
        orig = sys.modules.get('lib.agent_core.store')
        sys.modules['lib.agent_core.store'] = fake_mod
        try:
            # Re-sync the SAME two engine turns (idempotent incremental sync).
            engine_turns = [persisted[1], persisted[2]]
            _sync_endpoint_turns_to_conversation(
                {'id': 'tid12345', 'convId': 'c1'}, engine_turns)
        finally:
            if orig is not None:
                sys.modules['lib.agent_core.store'] = orig
            else:
                del sys.modules['lib.agent_core.store']

        out = captured.get('msgs')
        self.assertIsNotNone(out)
        vu_rows = [m for m in out if m.get('_isVirtualUser')]
        self.assertEqual(len(vu_rows), 1,
                         'VU row must not be duplicated by incremental sync')
        # Base = just the human ask (1 msg) + the 2 engine turns = 3.
        self.assertEqual(len(out), 3)

    def test_historical_collapse_preserves_vu_instructions(self):
        """A historical flow-autopilot run must NOT be flattened to one worker
        output — the VU instructions are real user turns and must survive into
        follow-up context (parity with the live autopilot path)."""
        from lib.tasks_pkg.conv_message_builder._dedup import (
            _collapse_historical_endpoint_sessions,
        )
        vu_text = 'Stop analyzing and execute.'
        src = [
            {'role': 'user', 'content': 'the ask'},
            {'role': 'assistant', 'content': 'analysis', '_epIteration': 1},
            {'role': 'user', 'content': vu_text, '_isVirtualUser': True,
             '_msgId': 'vu-1'},
            {'role': 'assistant', 'content': 'did edit', '_epIteration': 2},
            # A NON-endpoint follow-up makes the above a HISTORICAL run.
            {'role': 'user', 'content': 'follow-up question'},
        ]
        out = _collapse_historical_endpoint_sessions(src)
        user_texts = [m.get('content') for m in out if m.get('role') == 'user']
        self.assertIn(vu_text, user_texts,
                      'VU instruction lost when collapsing a historical run')


if __name__ == '__main__':
    unittest.main()
