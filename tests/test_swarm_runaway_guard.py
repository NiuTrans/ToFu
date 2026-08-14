"""Production-path guard: the swarm SubAgent cannot spin forever.

WHY
---
2026-07-27 incident: sub-agent `agent-researcher-6b62ec57` ran 26,683,114
rounds in 3.5h and wrote 53,366,229 lines = 9.1 GB into logs/app.log (96% of
the whole file). The 9 GB was the SYMPTOM; the defect is that NOTHING in the
production path detected semantic non-progress. The stub dispatcher in the incident
merely made it fast (2100 rounds/s); against a real gateway the same loop
does not stop either — it just burns money more slowly.

The chassis breaker (`max_consecutive_no_progress_rounds`, added alongside
this suite) is inert until a caller passes it, and a wall-clock net that
defaults to "off" is not a net. These tests pin BOTH ends:

  1. the chassis breaker is actually WIRED by SubAgent (not left at 0), and
  2. `SubTaskSpec` no longer exposes a numeric tool-round ceiling.

NEUTER evidence expected: removing the
``max_consecutive_no_progress_rounds=`` argument from the ``run_agent_loop``
call in ``lib/swarm/agent.py`` must make ``test_wedged_subagent_halts`` hang
→ its own bounded dispatcher raises, turning the test red.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Hard ceiling for the replay: far above any legitimate breaker threshold,
# far below the 2**30 that made the incident unbounded in practice.
_RUNAWAY_TRIPWIRE = 5000


class _Runaway(Exception):
    """Raised by the fake dispatcher when the loop refuses to stop."""


def _mk_agent(dispatch_fn, **spec_kw):
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='researcher', objective='incident replay',
                       **spec_kw)
    agent = SubAgent(
        spec,
        parent_task={},
        all_tools=[],
        model='replay-model',
        thinking_enabled=False,
        build_body_fn=lambda **kw: dict(kw),
        dispatch_stream_fn=dispatch_fn,
    )
    agent._execute_tool_calls = lambda tool_calls, round_num: None
    return agent


def _wedged_dispatch(counter):
    """The exact incident shape: identical tool call, empty content, forever."""
    def dispatch(body, **kw):
        counter['n'] += 1
        if counter['n'] > _RUNAWAY_TRIPWIRE:
            raise _Runaway(
                f'sub-agent ran {counter["n"]} identical rounds — nothing '
                f'bounded the loop (this is the 2026-07-27 runaway)')
        return ({'role': 'assistant', 'content': '',
                 'tool_calls': [{'id': 't1', 'function': {
                     'name': 'web_search', 'arguments': '{}'}}]},
                'tool_calls', {'total_tokens': 2})
    return dispatch


class TestSubAgentRunawayGuard(unittest.TestCase):

    def test_wedged_subagent_halts(self):
        """A sub-agent spawned with DEFAULTS must not spin forever."""
        counter = {'n': 0}
        agent = _mk_agent(_wedged_dispatch(counter))
        try:
            agent._run_loop(time.time())
        except _Runaway as e:
            self.fail(str(e))
        self.assertLess(counter['n'], _RUNAWAY_TRIPWIRE,
                        'loop must be bounded by the no-progress breaker')

    def test_defaults_cannot_be_unlimited_and_untimed(self):
        """Defaults must never yield an agent NOTHING can stop.

        The original form of this test required ``timeout_seconds != 0``, i.e.
        it demanded a WALL CLOCK specifically. That mechanism was measured to
        fail in both directions and has been replaced, so the assertion is now
        written against the PROPERTY the incident actually needs — "a default
        agent is bounded" — rather than against one particular implementation
        of it:

          * the wall clock could not catch the hang it was added for: it is
            evaluated only in ``before_round``, so an agent blocked inside a
            tool never reaches it (measured: >1h in a ``pytest`` child, never
            tripped);
          * and it DID cut down healthy long agents, because it keys on total
            runtime (measured legitimate runs: 1809s / 1846s / 1903s).

        The bounds that replace it key on BEHAVIOUR, so they bite the wedged
        shape without touching a productive one. Both must stay wired:
        ``test_wedged_subagent_halts`` above is the live proof that a
        DEFAULT-constructed agent still halts.
        """
        from lib.swarm.agent import SubAgent
        from lib.swarm.liveness import ProgressBeacon
        from lib.swarm.types import SubTaskSpec
        spec = SubTaskSpec(role='researcher', objective='x')

        # Bound #1 — the chassis no-progress breaker is wired, not left at 0.
        self.assertGreater(SubAgent._MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS, 0,
                           'no-progress breaker disabled — a wedged agent '
                           'would spin exactly as in the 2026-07-27 incident')
        # Bound #2 — a stall check exists and is finite.
        beacon = ProgressBeacon()
        self.assertGreater(beacon.stall_timeout, 0,
                           'stall timeout disabled — total silence would '
                           'never halt the agent')
        self.assertLess(beacon.stall_timeout, float('inf'))
        # An explicit wall clock remains OPTIONAL, and must not be resurrected
        # as a silent default (that is the reaping bug, not a safety net).
        self.assertEqual(spec.timeout_seconds, 0,
                         'a default wall-clock ceiling is back — it reaps '
                         'productive long agents and cannot see tool hangs')

    def test_removed_max_rounds_is_strictly_rejected(self):
        from lib.swarm.types import SubTaskSpec
        with self.assertRaises(TypeError):
            SubTaskSpec(role='researcher', objective='x', max_rounds=3)
        with self.assertRaises(ValueError):
            SubTaskSpec.from_dict({
                'role': 'researcher', 'objective': 'x', 'max_rounds': 3})

    def test_productive_agent_unaffected(self):
        """An agent making real progress must reach its natural answer."""
        from lib.swarm.types import SubAgentStatus
        seq = [
            ({'role': 'assistant', 'content': '',
              'tool_calls': [{'id': 'a', 'function': {
                  'name': 'web_search', 'arguments': '{"q":"1"}'}}]},
             'tool_calls', {'total_tokens': 2}),
            ({'role': 'assistant', 'content': '',
              'tool_calls': [{'id': 'b', 'function': {
                  'name': 'fetch_url', 'arguments': '{"u":"2"}'}}]},
             'tool_calls', {'total_tokens': 2}),
            ({'role': 'assistant',
              'content': 'the substantive final answer for the owner'},
             'stop', {'total_tokens': 2}),
        ]
        counter = {'n': 0}

        def dispatch(body, **kw):
            m = seq[counter['n']]
            counter['n'] += 1
            return m

        agent = _mk_agent(dispatch)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(counter['n'], 3)
        self.assertIn('substantive', agent.result.final_answer)


if __name__ == '__main__':
    unittest.main(verbosity=2)
