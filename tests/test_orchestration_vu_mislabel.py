"""tests/test_orchestration_vu_mislabel.py — VU turns must not be mislabeled.

Root-cause regression guard for the "编排里的自动驾驶比模式开关更蠢" bug:
when autopilot runs through the FlowExecutor engine (the "编排流程" dropdown),
the flow event adapter used to stamp EVERY user-side turn with
``_isFlowReview`` — including the virtual_user (VU) turns. That marker
makes ``_transform_messages`` (the LLM context builder) SKIP the row
(``_transform.py``: ``if msg.get('_isFlowReview'): continue``). So the
VU's instruction ("stop analyzing and execute — here is the checklist")
was SILENTLY DROPPED from the model's context, starving the next worker
turn. This is a correctness bug, not a cosmetic label.

These tests assert the fix along the whole marker chain:
  1. adapter stamps ``_isVirtualUser`` (not ``_isFlowReview``) for a
     virtual_user node, and NEVER stamps a VU row with an endpoint marker;
  2. such a VU row SURVIVES ``_transform_messages`` (reaches context);
  3. a critic flow (control) still stamps ``_isFlowReview`` and that row
     is still correctly SKIPPED by ``_transform_messages``.
"""

import unittest

import pytest

from lib.orchestration._builtin_definitions import build_autopilot_definition
from lib.orchestration_chat_flow_adapter import FlowEventAdapter
from lib.orchestration_engine import FlowExecutor
from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
from tests.support.orchestration_definitions import (
    build_verifier_loop_definition,
)


def _run(defn, runner):
    adapter = FlowEventAdapter()
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
    def test_vu_turn_marked_virtual_user_not_flow_review(self):
        msgs = _run(build_autopilot_definition(max_iterations=4),
                    _autopilot_runner())
        vu_rows = [m for m in msgs
                   if m.get('role') == 'user' and m.get('content')]
        self.assertTrue(vu_rows, 'expected at least one VU user-side turn')
        for m in vu_rows:
            self.assertTrue(m.get('_isVirtualUser'),
                            'VU turn must carry _isVirtualUser')
            self.assertFalse(m.get('_isFlowReview'),
                             'VU turn must NOT carry _isFlowReview '
                             '(that marker makes _transform skip it)')
            # It must carry NONE of the three context-skip markers.
            self.assertFalse(m.get('_flowIteration'))
            self.assertFalse(m.get('_isFlowPlanner'))
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
            {'role': 'assistant', 'content': 'analysis only', '_flowIteration': 1},
            {'role': 'user', 'content': vu_text, '_isVirtualUser': True,
             '_msgId': 'vu-1'},
        ]
        out = _transform_messages(raw, {})
        user_texts = [m.get('content') for m in out if m.get('role') == 'user']
        self.assertIn(vu_text, user_texts,
                      'VU instruction was dropped from LLM context')

    def test_critic_control_still_marked_and_still_skipped(self):
        # Adapter still stamps critic reviews _isFlowReview.
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

        msgs = _run(build_verifier_loop_definition(max_iterations=3), runner)
        critics = [m for m in msgs if m.get('role') == 'user']
        self.assertTrue(critics)
        for m in critics:
            self.assertTrue(m.get('_isFlowReview'))
            self.assertFalse(m.get('_isVirtualUser'))

        # And a critic row is STILL skipped by the context builder (its
        # feedback is injected via a different mechanism in Flow execution).
        raw = [
            {'role': 'user', 'content': 'do the work'},
            {'role': 'assistant', 'content': 'worked', '_flowIteration': 1},
            {'role': 'user', 'content': 'CRITIC FEEDBACK TEXT',
             '_isFlowReview': True, '_flowIteration': 1},
        ]
        out = _transform_messages(raw, {})
        user_texts = [m.get('content') for m in out if m.get('role') == 'user']
        self.assertNotIn('CRITIC FEEDBACK TEXT', user_texts,
                         'critic review must remain skipped from context')


class VuConsumerTest(unittest.TestCase):
    """The marker change must be honored by the DOWNSTREAM consumers too,
    not just the producer + context builder."""

    def test_sync_boundary_forwards_vu_as_an_explicit_visible_turn(self):
        """Endpoint projection preserves VU identity at the turn authority."""
        from lib.orchestration_chat_turn_sync import (
            sync_flow_turns_to_conversation,
        )
        import lib.turn_lifecycle as turn_lifecycle

        engine_turns = [
            {'role': 'assistant', 'content': 'w1', '_flowIteration': 1},
            {'role': 'user', 'content': 'keep going', '_isVirtualUser': True,
             '_msgId': 'vu-1'},
        ]
        captured = {}

        def capture(task, messages):
            captured['task'] = task
            captured['messages'] = messages

        original = turn_lifecycle.sync_visible_run_turns
        turn_lifecycle.sync_visible_run_turns = capture
        try:
            sync_flow_turns_to_conversation(
                {
                    'id': 'task-1',
                    'convId': 'conversation-1',
                    '_turnId': 'turn-1',
                    '_attemptId': 'attempt-1',
                },
                engine_turns,
            )
        finally:
            turn_lifecycle.sync_visible_run_turns = original

        vu_rows = [
            message for message in captured['messages']
            if message.get('_isVirtualUser')
        ]
        self.assertEqual(len(vu_rows), 1,
                         'VU row must reach the turn authority exactly once')
        self.assertEqual(captured['messages'], engine_turns)

    def test_historical_collapse_preserves_vu_instructions(self):
        """A historical flow-autopilot run must NOT be flattened to one worker
        output — the VU instructions are real user turns and must survive into
        follow-up context (parity with the live autopilot path)."""
        from lib.tasks_pkg.conv_message_builder._dedup import (
            _collapse_historical_flow_sessions,
        )
        vu_text = 'Stop analyzing and execute.'
        src = [
            {'role': 'user', 'content': 'the ask'},
            {'role': 'assistant', 'content': 'analysis', '_flowIteration': 1},
            {'role': 'user', 'content': vu_text, '_isVirtualUser': True,
             '_msgId': 'vu-1'},
            {'role': 'assistant', 'content': 'did edit', '_flowIteration': 2},
            # A NON-endpoint follow-up makes the above a HISTORICAL run.
            {'role': 'user', 'content': 'follow-up question'},
        ]
        out = _collapse_historical_flow_sessions(src)
        user_texts = [m.get('content') for m in out if m.get('role') == 'user']
        self.assertIn(vu_text, user_texts,
                      'VU instruction lost when collapsing a historical run')


if __name__ == '__main__':
    unittest.main()
