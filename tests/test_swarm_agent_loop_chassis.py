"""Behavior-parity tests: swarm SubAgent loop on the run_agent_loop chassis.

WHY
---
Charter iron rule (2026-07-27) execution: lib/swarm/agent.py's private
while-loop was migrated onto lib/agent_loop.run_agent_loop (the FIRST legacy
loop the chassis absorbs — endpoint/orchestrator follow). The chassis owns
the round loop + abort checks + the before_round halt seam (timeout); swarm
keeps its specifics in hooks. These tests pin the SIX externally observable
paths of the old loop so the migration is provably behavior-preserving:

  1. final answer (natural completion)      → COMPLETED + answer
  2. tool round then final answer           → batch hook once, then COMPLETED
  3. max_rounds exhausted                   → COMPLETED + [Partial …] answer
  4. abort after a tool round               → CANCELLED + partial answer
  5. wall-clock timeout (before_round halt) → COMPLETED + timeout event
  6. LLM error on round 1                   → FAILED + error message

NEUTER evidence (manual, 2026-07-27):
  * dropping the ``before_round=_before_round`` wiring makes test 5 HANG —
    the timeout halt never fires and the fake's infinite tool-call loop has
    no other stop (the bite is a test-timeout, proving the wiring is the
    only wall-clock guard);
  * swapping ``execute_tools=`` for the per-tool ``execute_tool=`` path
    turns test 2 red (the batch hook never fires — parallel-pool contract
    broken);
  * NOTE on ``tools_terminal_round``: flipping it is INVISIBLE at the swarm
    level, because swarm's dispatch hook deliberately ignores the
    chassis-offered tools (it builds its body from self.tools). The flip is
    therefore pinned at the chassis level instead —
    tests/test_agent_loop.py::test_tools_terminal_round_off_offers_tools_
    every_round. (First draft of this docstring claimed a swarm-level
    dispatch-count bite; that claim was measured WRONG and corrected.)
"""

from __future__ import annotations

import os
import sys
import time
import unittest

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, '..'))


def _mk_agent(*, dispatch_fn, max_rounds=0, timeout_seconds=0,
              abort_check=None, events=None):
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='researcher', objective='parity-test objective',
                       max_rounds=max_rounds, timeout_seconds=timeout_seconds)
    agent = SubAgent(
        spec,
        parent_task={},               # no id → request snapshots are no-ops
        all_tools=[],
        model='parity-model',
        thinking_enabled=False,
        on_event=events,
        abort_check=abort_check,
        build_body_fn=lambda **kw: dict(kw),
        dispatch_stream_fn=dispatch_fn,
    )
    # Patch the parallel-pool seam: record the batch and append tool-result
    # messages exactly like the real _execute_tool_calls (order preserved).
    agent._tool_batches = []

    def _fake_exec(tool_calls, round_num):
        agent._tool_batches.append((round_num, list(tool_calls)))
        for tc in tool_calls:
            agent.messages.append({
                'role': 'tool', 'tool_call_id': tc.get('id', 'x'),
                'content': f'result:{tc["function"]["name"]}'})
    agent._execute_tool_calls = _fake_exec
    return agent


def _tc(name='web_search', _id='t1'):
    return {'id': _id, 'function': {'name': name, 'arguments': '{}'}}


def _final_msg(text):
    return {'role': 'assistant', 'content': text}, 'stop', \
        {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}


def _tool_msg(calls):
    return {'role': 'assistant', 'content': '', 'tool_calls': calls}, \
        'tool_calls', {'prompt_tokens': 1, 'completion_tokens': 1,
                      'total_tokens': 2}


class TestSwarmOnChassis(unittest.TestCase):

    def test_final_answer_completes(self):
        from lib.swarm.types import SubAgentStatus
        disp = {'n': 0}

        def dispatch(body, **kw):
            disp['n'] += 1
            return _final_msg('the answer is 42 — long enough to matter')

        agent = _mk_agent(dispatch_fn=dispatch)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(agent.result.final_answer,
                         'the answer is 42 — long enough to matter')
        self.assertEqual(agent.result.rounds_used, 1)
        self.assertEqual(disp['n'], 1)
        self.assertEqual(agent._tool_batches, [])

    def test_tool_round_then_final_uses_batch_hook(self):
        from lib.swarm.types import SubAgentStatus
        seq = [_tool_msg([_tc('web_search', 't1'), _tc('fetch_url', 't2')]),
               _final_msg('done after tools — a substantive answer body')]
        disp = {'n': 0}

        def dispatch(body, **kw):
            m = seq[disp['n']]
            disp['n'] += 1
            return m

        agent = _mk_agent(dispatch_fn=dispatch)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(disp['n'], 2)
        # The batch hook fired ONCE with BOTH tool calls (parallel-pool
        # contract), not twice per-tool.
        self.assertEqual(len(agent._tool_batches), 1)
        rnd, calls = agent._tool_batches[0]
        self.assertEqual(rnd, 1)
        self.assertEqual([c['id'] for c in calls], ['t1', 't2'])
        # Tool results appended before the second dispatch.
        roles = [m['role'] for m in agent.messages]
        self.assertEqual(roles.count('tool'), 2)

    def test_max_rounds_exhausted_extracts_partial(self):
        from lib.swarm.types import SubAgentStatus
        disp = {'n': 0}

        def dispatch(body, **kw):
            disp['n'] += 1
            # Round 1 leaves substantive content in history (for the partial
            # extraction), then keeps demanding tools forever.
            msg, sr, u = _tool_msg([_tc()])
            msg['content'] = 'working on it — substantive partial content here'
            return msg, sr, u

        agent = _mk_agent(dispatch_fn=dispatch, max_rounds=2)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(disp['n'], 2, 'must run EXACTLY max_rounds rounds — '
                         'a tools_terminal_round=True regression adds a 3rd')
        self.assertEqual(agent.result.rounds_used, 2)
        self.assertTrue(agent.result.final_answer.startswith('[Partial'),
                        agent.result.final_answer[:80])
        self.assertIn('Max rounds (2) reached', agent.result.final_answer)
        self.assertEqual(len(agent._tool_batches), 2)

    def test_abort_after_tool_round_cancels(self):
        from lib.swarm.types import SubAgentStatus
        flag = {'v': False}
        disp = {'n': 0}

        def dispatch(body, **kw):
            disp['n'] += 1
            msg, sr, u = _tool_msg([_tc()])
            msg['content'] = 'partial content long enough to be rescued here'
            return msg, sr, u

        agent = _mk_agent(dispatch_fn=dispatch,
                          abort_check=lambda: flag['v'])
        # Flip abort during the first tool batch (the old post-tools check,
        # now covered by the chassis' next before-round check).
        real_exec = agent._execute_tool_calls

        def exec_then_abort(tool_calls, round_num):
            real_exec(tool_calls, round_num)
            flag['v'] = True
        agent._execute_tool_calls = exec_then_abort

        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.CANCELLED.value)
        self.assertEqual(disp['n'], 1, 'no fresh round after abort')
        self.assertIn('cancelled', agent.result.final_answer)

    def test_timeout_halts_via_before_round(self):
        from lib.swarm.types import SubAgentStatus
        events = []
        disp = {'n': 0}

        def dispatch(body, **kw):
            disp['n'] += 1
            return _tool_msg([_tc()])

        # timeout_seconds=-1 → "already timed out" at the first round top —
        # deterministic, no sleeping.
        agent = _mk_agent(dispatch_fn=dispatch, timeout_seconds=-1,
                          events=lambda *a, **kw: events.append((a, kw)))
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(disp['n'], 0, 'timeout must halt BEFORE round 1')
        self.assertIn('timed out', agent.result.final_answer)
        # The timeout event reached the parent stream (SwarmEvent namespaces
        # the raw 'timeout' type to 'swarm_timeout').
        self.assertTrue(
            any(a and isinstance(a[0], dict)
                and 'timeout' in str(a[0].get('type', ''))
                for a, _ in events),
            f'no timeout event in {events!r}')

    def test_llm_error_round_one_fails(self):
        from lib.swarm.types import SubAgentStatus

        def dispatch(body, **kw):
            raise RuntimeError('gateway exploded')

        agent = _mk_agent(dispatch_fn=dispatch)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.FAILED.value)
        self.assertIn('LLM call failed at round 1',
                      agent.result.error_message or '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
