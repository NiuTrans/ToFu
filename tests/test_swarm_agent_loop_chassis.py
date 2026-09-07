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
  3. many productive tool rounds            → tools stay available, then complete
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
  * tool availability is pinned both here and at the chassis level; there is
    no terminal tool-less round or numeric ceiling.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, '..'))


def _mk_agent(*, dispatch_fn, timeout_seconds=0,
              abort_check=None, events=None):
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='researcher', objective='parity-test objective',
                       timeout_seconds=timeout_seconds)
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


def _tc(name='web_search', _id='t1', arguments='{}'):
    return {'id': _id, 'function': {'name': name, 'arguments': arguments}}


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

    def test_stream_log_creates_parent_only_when_output_arrives(self):
        def dispatch(body, **kwargs):
            kwargs['on_thinking']('measured thought; ')
            kwargs['on_content']('streamed final answer')
            return _final_msg('streamed final answer')

        with tempfile.TemporaryDirectory(prefix='swarm-lazy-log-') as root:
            output_path = Path(root) / 'session' / 'worker.log'
            agent = _mk_agent(dispatch_fn=dispatch)
            agent.output_file = str(output_path)
            self.assertFalse(output_path.parent.exists())

            agent._run_loop(time.time())

            self.assertEqual(
                output_path.read_text(encoding='utf-8'),
                'measured thought; streamed final answer',
            )

    def test_rehydrated_round_number_continues_from_checkpoint(self):
        events = []

        def dispatch(body, **kw):
            return _final_msg('resumed agent completed normally')

        agent = _mk_agent(dispatch_fn=dispatch, events=events.append)
        agent._round_offset = 5
        agent.result.rounds_used = 5
        agent._run_loop(time.time())

        self.assertEqual(agent.result.rounds_used, 6)
        completed = [e for e in events if e.get('phase') == 'done']
        self.assertEqual(completed[-1]['roundNum'], 6)

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

    def test_many_tool_rounds_complete_naturally(self):
        from lib.swarm.types import SubAgentStatus
        disp = {'n': 0}

        def dispatch(body, **kw):
            disp['n'] += 1
            if disp['n'] > 12:
                return ({'role': 'assistant', 'content': 'finished naturally',
                         'tool_calls': []}, 'stop', {})
            n = disp['n']
            msg, sr, u = _tool_msg([
                _tc(_id=f't{n}', arguments=f'{{"query":"step-{n}"}}')])
            return msg, sr, u

        agent = _mk_agent(dispatch_fn=dispatch)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(disp['n'], 13)
        self.assertEqual(agent.result.rounds_used, 13)
        self.assertEqual(agent.result.final_answer, 'finished naturally')
        self.assertEqual(len(agent._tool_batches), 12)

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

    def test_llm_error_with_only_placeholder_stays_failed(self):
        """Round-2+ LLM failure with NO substantive history: the synthesized
        'No substantive answer' placeholder must NOT promote the run to
        COMPLETED — the real error_message has to reach the transcript."""
        from lib.swarm.types import SubAgentStatus
        calls = {'n': 0}

        def dispatch(body, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                return _tool_msg([_tc()])
            raise RuntimeError('schema rejected (HTTP 400)')

        agent = _mk_agent(dispatch_fn=dispatch)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.FAILED.value)
        self.assertIn('LLM call failed at round 2',
                      agent.result.error_message or '')
        self.assertIn('No substantive answer',
                      agent.result.final_answer or '')

    def test_llm_error_with_genuine_partial_stays_failed(self):
        from lib.swarm.types import SubAgentStatus
        calls = {'n': 0}

        def dispatch(body, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                msg, sr, u = _tool_msg([_tc()])
                msg['content'] = ('partial findings substantial enough '
                                  'to be worth keeping')
                return msg, sr, u
            raise RuntimeError('schema rejected (HTTP 400)')

        agent = _mk_agent(dispatch_fn=dispatch)
        agent._run_loop(time.time())
        self.assertEqual(agent.result.status, SubAgentStatus.FAILED.value)
        self.assertIn('LLM call failed at round 2',
                      agent.result.error_message or '')
        self.assertIn('partial findings', agent.result.final_answer or '')
    def test_content_bearing_malformed_stream_never_completes(self):
        from lib.llm.stream_result import (
            ProviderStreamResult,
            ProviderStreamState,
            UnverifiedProviderStreamError,
        )
        from lib.swarm.types import SubAgentStatus

        def dispatch(body, **kw):
            on_content = kw.get('on_content')
            if on_content:
                on_content('safe prefix')
            return ProviderStreamResult(
                message={'role': 'assistant', 'content': 'safe prefix'},
                compatibility_finish_reason='stop',
                usage={},
                state=ProviderStreamState.MALFORMED_STREAM,
                malformed_frame_count=1,
            )

        agent = _mk_agent(dispatch_fn=dispatch)
        with self.assertRaises(UnverifiedProviderStreamError):
            agent._run_loop(time.time())

        self.assertEqual(agent.result.status, SubAgentStatus.PENDING.value)
        self.assertEqual(agent._tool_batches, [])
        self.assertFalse(any(
            message.get('role') == 'assistant'
            for message in agent.messages))


if __name__ == '__main__':
    unittest.main(verbosity=2)
