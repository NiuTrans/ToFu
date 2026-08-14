"""Guard: the per-round log line states the live semantic safety breakers.

WHY
---
2026-07-27 incident review. The swarm per-round heartbeat printed

    ── Round 4/∞ START ── messages=8

After the no-progress breaker landed, the line must show the live protections:
``_MAX_CONSECUTIVE_NO_PROGRESS_ROUNDS`` (10 identical tool-call rounds) and
``SubTaskSpec.timeout_seconds`` (1800s by default).

That matters beyond cosmetics: during the incident the ONLY operator-facing
signal was this line, repeated 26.7 million times, and it said the loop was
unbounded — which was true then and is false now. A protection that is
invisible in the logs cannot be trusted by the person reading them, and
"is the breaker actually live in this process?" is exactly the question a
post-restart acceptance check has to answer from the log alone.

So the round line renders ``∞(np=10,t=1800s)``: normal tool work is not
numerically capped, while semantic and optional wall-clock breakers remain
observable.

This is a LOG-CONTRACT test: it asserts the rendered string, because the
string IS the operator interface here.
"""

from __future__ import annotations

import os
import sys
import unittest

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_agent(**spec_kw):
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='researcher', objective='bounds render', **spec_kw)
    return SubAgent(
        spec, parent_task={}, all_tools=[], model='m',
        thinking_enabled=False,
        build_body_fn=lambda **kw: dict(kw),
        dispatch_stream_fn=lambda body, **kw: (
            {'role': 'assistant', 'content': 'done'}, 'stop', {}),
    )


class TestRoundBoundsRendering(unittest.TestCase):

    def test_safety_label_renders_effective_breakers(self):
        agent = _mk_agent(timeout_seconds=1800)
        label = agent._round_safety_label()
        self.assertNotEqual(
            label, '\u221e',
            'a bare "∞" hides the no-progress breaker and the wall-clock '
            'timeout — both are live bounds')
        self.assertIn('np=10', label,
                      f'no-progress threshold missing from {label!r}')
        self.assertIn('1800', label,
                      f'wall-clock timeout missing from {label!r}')

    def test_without_timeout_still_shows_the_breaker(self):
        """A caller may deliberately pass timeout_seconds=0; the breaker is
        still the bound and must remain visible."""
        agent = _mk_agent(timeout_seconds=0)
        label = agent._round_safety_label()
        self.assertIn('np=10', label)
        self.assertNotIn('t=0', label,
                         'a disabled wall clock should not be advertised '
                         'as a bound')

    def test_label_is_used_by_the_round_start_line(self):
        """The renderer must actually be wired into the logged line."""
        import inspect

        from lib.swarm import agent as agent_mod
        src = inspect.getsource(agent_mod)
        self.assertNotIn('max_rounds', src,
                         'the retired numeric round ceiling leaked back in')
        self.assertIn('_round_safety_label()', src,
                      'the round-start line does not call the renderer')


if __name__ == '__main__':
    unittest.main(verbosity=2)
