"""Per-round thinking/reasoning must be captured even without interstitial prose.

Root cause (reported bug — "the Content and Reasoning Content of intermediate
rounds of Assistant is still being lost" in the inline tool-timeline):
``parse_tool_calls`` stamped a round's per-round prose onto its first tool-round
entry, but the thinking/signature stamp was NESTED INSIDE a guard requiring
``_assistant_content`` (the interstitial prose) to be non-empty::

    if _assistant_content and not _ac_tagged:      # ← gated on CONTENT
        round_entry['assistantContent'] = _assistant_content
        if _assistant_thinking:
            round_entry['thinking'] = _assistant_thinking   # unreachable when no content

A reasoning model routinely emits THINKING then calls a tool directly with NO
interstitial narration (``_assistant_content == ''``) — the common multi-round
shape. In that case the whole block was skipped, so ``round['thinking']`` was
never stamped. The live bubble briefly showed it (the frontend ``delta_reset``
handler stamps content/thinking independently), but the terminal turn
projection is built from ``task['segments']`` via ``assemble_segments``, which
reads ``round['thinking']``. Without the stamp, the intermediate round's
reasoning vanished on settlement and reload.

Fix: capture content / thinking / signature INDEPENDENTLY in both the
early-announce and the normal branch (mirror the frontend's independent
handling).

Run directly (conda env pytest is flaky):

    python3 tests/test_parse_thinking_only_capture.py
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls
from lib.tasks_pkg.segments import assemble_segments, SEG_THINKING, SEG_TEXT

pytestmark = pytest.mark.unit


def _schema(names):
    return [{'type': 'function', 'function': {'name': n, 'parameters': {}}}
            for n in names]


def _make_task(tool_names):
    return {
        'id': 'task_thinkcap_' + 'x' * 6,
        'convId': 'convthinkcap',
        'model': 'test-model',
        'events': [],
        'events_lock': threading.Lock(),
        'toolRounds': [],
        'aborted': False,
        '_tool_schema': _schema(tool_names),
    }


def _assistant(tool_calls, content='', reasoning='', signature=''):
    m = {'content': content, 'tool_calls': tool_calls}
    if reasoning:
        m['reasoning_content'] = reasoning
    if signature:
        m['thinking_signature'] = signature
    return m


def _tc(name, args='{}', tc_id=None):
    return {'id': tc_id or ('call_' + name), 'type': 'function',
            'function': {'name': name, 'arguments': args}}


class TestThinkingOnlyRoundCapture(unittest.TestCase):
    def test_thinking_only_round_captures_reasoning(self):
        """A round with reasoning but NO interstitial content still stamps
        thinking (+signature) onto its first tool round."""
        task = _make_task(['read_files'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('read_files', '{"path": "a.py"}')],
                       content='',            # ← NO interstitial prose
                       reasoning='I should read the file first.',
                       signature='opaque-sig-x'),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        round_entry = parsed[0][5]
        self.assertEqual(round_entry.get('thinking'), 'I should read the file first.')
        self.assertEqual(round_entry.get('thinkingSignature'), 'opaque-sig-x')
        # No content was emitted, so assistantContent must be absent (not '').
        self.assertNotIn('assistantContent', round_entry)

    def test_thinking_survives_into_segments(self):
        """The captured thinking is what assemble_segments emits as the round's
        SEG_THINKING segment — proving the settled/committed render carries it."""
        task = _make_task(['grep_search'])
        parse_tool_calls(
            _assistant([_tc('grep_search', '{"pattern": "bug"}')],
                       content='', reasoning='Reasoning about round zero.'),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        # Terminal content (the deliverable answer) for a realistic finished turn.
        task['content'] = 'The answer.'
        task['thinking'] = ''
        segs = assemble_segments(task)
        batch0_think = [s for s in segs
                        if s.get('type') == SEG_THINKING and s.get('llmRound') == 0]
        self.assertEqual(len(batch0_think), 1)
        self.assertEqual(batch0_think[0]['text'], 'Reasoning about round zero.')

    def test_content_and_thinking_both_captured_when_present(self):
        """Regression guard: the pre-existing behavior (both present) still works."""
        task = _make_task(['web_search'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('web_search', '{"query": "x"}')],
                       content='Let me search.',
                       reasoning='Search first.'),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        round_entry = parsed[0][5]
        self.assertEqual(round_entry.get('assistantContent'), 'Let me search.')
        self.assertEqual(round_entry.get('thinking'), 'Search first.')

    def test_content_only_round_still_captures_content(self):
        """Content but no reasoning — the narration is captured, thinking absent."""
        task = _make_task(['read_files'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('read_files', '{"path": "a.py"}')],
                       content='Let me read it.', reasoning=''),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        round_entry = parsed[0][5]
        self.assertEqual(round_entry.get('assistantContent'), 'Let me read it.')
        self.assertNotIn('thinking', round_entry)

    def test_only_first_entry_of_batch_tagged(self):
        """Two tool calls in one round → prose tagged onto the FIRST only."""
        task = _make_task(['grep_search', 'read_files'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('grep_search', '{"pattern": "x"}', tc_id='c1'),
                        _tc('read_files', '{"path": "a.py"}', tc_id='c2')],
                       content='', reasoning='Batch reasoning.'),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        first, second = parsed[0][5], parsed[1][5]
        self.assertEqual(first.get('thinking'), 'Batch reasoning.')
        self.assertNotIn('thinking', second)

    def test_NC_regate_on_content_loses_thinking(self):
        """NEUTER: re-gate the capture on content (the old bug) and prove a
        thinking-only round loses its reasoning — the guard is load-bearing.

        Simulated at the data level (the fix decoupled the two fields): with the
        old ``if _assistant_content and not _ac_tagged`` gate, a round whose
        content is empty would skip the whole block, so thinking would NOT be
        stamped. We emulate that outcome and assert it is the broken state the
        fix prevents."""
        task = _make_task(['read_files'])
        parsed, _ = parse_tool_calls(
            _assistant([_tc('read_files', '{"path": "a.py"}')],
                       content='', reasoning='Lost reasoning.'),
            task, round_num=0, tool_round_num=0, project_enabled=False,
        )
        round_entry = parsed[0][5]
        # With the FIX: thinking is present.
        self.assertEqual(round_entry.get('thinking'), 'Lost reasoning.')
        # NEUTER: drop it (emulate the old content-gated skip) → segment loses it.
        del round_entry['thinking']
        task['content'] = 'Answer.'
        task['thinking'] = ''
        segs = assemble_segments(task)
        batch0_think = [s for s in segs
                        if s.get('type') == SEG_THINKING and s.get('llmRound') == 0]
        self.assertEqual(batch0_think, [],
                         'without the round thinking stamp, the segment timeline '
                         'has NO reasoning for round 0 — the reported bug')


if __name__ == '__main__':
    unittest.main(verbosity=2)
