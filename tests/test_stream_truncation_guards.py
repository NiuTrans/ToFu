# -*- coding: utf-8 -*-
"""Stream-truncation guards (G1/G2/G3/G4) — 2026-08-05 owner directive batch.

Root-cause context (see memory premature-close-root-cause): the example-corp
gateway severs SSE streams WITHOUT the terminal frames (375 events in one
month, all models, all ending on clean chunk boundaries). The retry layers
heal the empty-output cases, but four gaps were measured:

  G1  a cut landing MID-TOOL-ARGUMENTS left an unparseable ``arguments``
      string that the orchestrator proceeded to EXECUTE (or the sanitizer
      substituted ``{}`` — a tool running on empty/wrong args). 34 of 560
      anomaly dumps died inside tool args.
  G2  a cut WITH partial content failed the whole turn (abnormal_stop error
      envelope + whole-turn auto-retry wipe) when the user can only Retry /
      Continue anyway. Now a soft landing: premature_close finish tag, no
      error envelope, partial reply kept.
  G3  ``record_truncation`` cooled a slot only on 3 CONSECUTIVE truncations,
      but interleaved successes kept zeroing the streak — intermittent rot
      never cooled. Now a rolling 10-minute window also feeds the gate.
  G4  the async transport (httpx) ignored env ``no_proxy`` — internal
      gateways hairpinned through the corporate proxy while the sync path
      went direct. ``lib.proxy.async_proxy_for`` is now the single decision
      point, byte-consistent with the sync predicate.

Plus the swarm SubAgent coverage: a poisoned round (truncated tool args /
empty stream) was silently appended to history and its tool calls executed;
now it rides the chassis ``retry_bonus`` for a bounded transparent retry.

Run: pytest tests/test_stream_truncation_guards.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.agent_loop import unparseable_tool_calls  # noqa: E402
from lib.tasks_pkg.stream_handler import (  # noqa: E402
    _PREMATURE_RETRY_MAX_CLASSIC,
    analyse_stream_result,
)


# ─────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────

def _fresh_task(*, phase_counter=0, content='', thinking='',
                round_base_content=None, round_base_thinking=None):
    t = {
        'id': 'trunc-test',
        'aborted': False,
        'content': content,
        'thinking': thinking,
        'error': None,
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
    }
    if phase_counter is not None:
        t['_premature_retry_count_phase'] = phase_counter
    if round_base_content is not None:
        t['_round_base_content'] = round_base_content
    if round_base_thinking is not None:
        t['_round_base_thinking'] = round_base_thinking
    return t


class _no_sleep:
    """Patch the facade-routed backoff sleep away for the test duration."""

    def __enter__(self):
        import lib.tasks_pkg.stream_handler as sh
        self._sh = sh
        self._orig = sh._interruptible_sleep
        sh._interruptible_sleep = lambda seconds, task: None
        return self

    def __exit__(self, *exc):
        self._sh._interruptible_sleep = self._orig
        return False


def _tc(name, arguments, _id='t1'):
    return {'id': _id, 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


def _missing_done_usage(**over):
    u = {
        '_missing_done': True,
        '_stream_anomaly': True,
        'trace_id': 'M-TRUNC-TEST',
        'stream_elapsed_ms': 42000,
        '_chunks_received': 500,
    }
    u.update(over)
    return u


# ─────────────────────────────────────────────────────────────────────
#  unparseable_tool_calls (shared helper, lib/agent_loop.py)
# ─────────────────────────────────────────────────────────────────────

class TestUnparseableToolCalls(unittest.TestCase):

    def test_valid_args_pass(self):
        msg = {'tool_calls': [_tc('write_file', '{"path": "a.py"}')]}
        self.assertEqual(unparseable_tool_calls(msg), [])

    def test_truncated_args_flagged(self):
        msg = {'tool_calls': [_tc('write_file', '{"path": "a.py", "content": "ab')]}
        bad = unparseable_tool_calls(msg)
        self.assertEqual(len(bad), 1)
        self.assertIs(bad[0], msg['tool_calls'][0])

    def test_mixed_batch_flags_only_the_cut_one(self):
        good = _tc('read_files', '{"path": "x"}', _id='t1')
        bad = _tc('write_file', '{"path":', _id='t2')
        out = unparseable_tool_calls({'tool_calls': [good, bad]})
        self.assertEqual([tc['id'] for tc in out], ['t2'])

    def test_empty_args_are_not_truncation(self):
        # The accumulator normalizes '' → '{}' for genuine no-arg tools; an
        # empty string must NOT read as a cut.
        self.assertEqual(
            unparseable_tool_calls({'tool_calls': [_tc('list_dir', '')]}), [])

    def test_already_decoded_dict_args_pass(self):
        msg = {'tool_calls': [{'function': {'name': 'x', 'arguments': {'a': 1}}}]}
        self.assertEqual(unparseable_tool_calls(msg), [])

    def test_non_dict_msg_and_missing_calls(self):
        self.assertEqual(unparseable_tool_calls(None), [])
        self.assertEqual(unparseable_tool_calls({'role': 'assistant'}), [])


# ─────────────────────────────────────────────────────────────────────
#  G1 — orchestrator: truncated tool args → transparent retry, never execute
# ─────────────────────────────────────────────────────────────────────

class TestTruncatedToolArgsRetry(unittest.TestCase):

    def _analyse(self, task, msg, usage, round_num=1):
        return analyse_stream_result(
            assistant_msg=msg, last_finish_reason='stop', task=task,
            tid='trunct', model='kimi-k3', round_num=round_num,
            _premature_retry_count=0, messages=[], usage=usage)

    def test_corrupt_args_retry_and_reset_to_round_base(self):
        """The 16:11:41 production shape (tool_calls=1, stream lost [DONE]):
        corrupt args → retry, poisoned partial text reset to the round base,
        DELTA_RESET + retrying phase emitted, residue recorded for the
        shrink-convergent settle guards."""
        from lib.agent_core.events import EventType
        task = _fresh_task(content='PRIOR-PROSE poisoned-partial',
                           thinking='some thinking',
                           round_base_content='PRIOR-PROSE',
                           round_base_thinking='')
        msg = {'role': 'assistant', 'content': '',
               'tool_calls': [_tc('write_file', '{"path": "a.py", "cont')]}
        with _no_sleep():
            d = self._analyse(task, msg, _missing_done_usage())
        self.assertEqual(d['action'], 'continue')
        self.assertEqual(d['premature_retry_count'], 1)
        self.assertEqual(task['_premature_retry_count_phase'], 1)
        # Poisoned tail discarded, prior prose kept.
        self.assertEqual(task['content'], 'PRIOR-PROSE')
        self.assertEqual(task['thinking'], '')
        # Residue recorded so the settle guards allow the shrink-overwrite.
        self.assertEqual(task['_floor_retry_residue'][-1]['content'],
                         'PRIOR-PROSE poisoned-partial')
        types = [e.get('type') for e in task['events']]
        self.assertIn(EventType.DELTA_RESET, types)
        phases = [e for e in task['events'] if e.get('type') == 'phase']
        self.assertTrue(phases and phases[-1].get('bucket')
                        == 'truncated_tool_args', task['events'])

    def test_valid_args_proceed_despite_missing_done(self):
        """A cut after the last real chunk loses only terminal frames —
        every arguments string parses, so proceeding is provably safe."""
        task = _fresh_task(content='work so far',
                           round_base_content='', round_base_thinking='')
        msg = {'role': 'assistant', 'content': 'some narration',
               'tool_calls': [_tc('read_files', '{"path": "x"}')]}
        d = self._analyse(task, msg, _missing_done_usage())
        self.assertEqual(d['action'], 'proceed')
        self.assertIsNone(task['error'])
        self.assertEqual(task['events'], [])

    def test_guard_gated_on_missing_done(self):
        """Corrupt args WITHOUT _missing_done (e.g. a model glitch, not a
        transport cut) must not enter the retry bucket — the guard keys on
        data-loss evidence, not on parse failure alone."""
        task = _fresh_task()
        usage = _missing_done_usage()
        usage.pop('_missing_done')
        msg = {'role': 'assistant', 'content': '',
               'tool_calls': [_tc('write_file', '{"path":')]}
        d = self._analyse(task, msg, usage)
        self.assertEqual(d['action'], 'proceed')

    def test_exhausted_budget_surfaces_premature_close_envelope(self):
        task = _fresh_task(phase_counter=_PREMATURE_RETRY_MAX_CLASSIC)
        msg = {'role': 'assistant', 'content': '',
               'tool_calls': [_tc('write_file', '{"path":')]}
        with _no_sleep():
            d = analyse_stream_result(
                assistant_msg=msg, last_finish_reason='stop', task=task,
                tid='trunct', model='kimi-k3', round_num=3,
                _premature_retry_count=_PREMATURE_RETRY_MAX_CLASSIC,
                messages=[], usage=_missing_done_usage())
        self.assertEqual(d['action'], 'break')
        self.assertEqual(d['last_finish_reason'], 'premature_close')
        self.assertIsNotNone(task['error'])
        self.assertEqual(task['error'].get('kind'), 'premature_close')

    def test_no_round_base_stamps_still_retries(self):
        """Callers that never stamp a round base (paper/swarm legacy) retry
        without the content reset — no KeyError, no wipe."""
        task = _fresh_task(content='partial', thinking='')
        msg = {'role': 'assistant', 'content': '',
               'tool_calls': [_tc('write_file', '{"x":')]}
        with _no_sleep():
            d = self._analyse(task, msg, _missing_done_usage())
        self.assertEqual(d['action'], 'continue')
        self.assertEqual(task['content'], 'partial')  # untouched


# ─────────────────────────────────────────────────────────────────────
#  G2 — content-bearing stream anomaly: soft landing, not an error
# ─────────────────────────────────────────────────────────────────────

class TestPartialContentSoftLanding(unittest.TestCase):

    def test_partial_content_settles_premature_close_without_error(self):
        """Owner directive: the user sees the failure (premature_close finish
        tag = '网关中断 · 内容可能不完整') but the turn is NOT interrupted —
        no error envelope, no whole-turn auto-retry wiping the partial."""
        task = _fresh_task(content='an almost-complete answer body')
        msg = {'role': 'assistant',
               'content': 'an almost-complete answer body',
               'reasoning_content': ''}
        usage = {'_stream_anomaly': True, '_missing_done': True,
                 '_chunks_received': 900, 'stream_elapsed_ms': 176000,
                 'trace_id': 'M-PARTIAL'}
        d = analyse_stream_result(
            assistant_msg=msg, last_finish_reason='stop', task=task,
            tid='soft', model='kimi-k3', round_num=2,
            _premature_retry_count=0, messages=[], usage=usage)
        self.assertEqual(d['action'], 'break')
        self.assertEqual(d['last_finish_reason'], 'premature_close')
        self.assertIn('soft', d['loop_exit_reason'])
        self.assertIsNone(task['error'])  # no error card, no auto-retry

    def test_no_content_anomaly_keeps_honest_error(self):
        """Nothing streamed at all → there is no partial to preserve; the
        abnormal_stop envelope (and its turn-level auto-retry) stays."""
        task = _fresh_task()
        msg = {'role': 'assistant', 'content': '',
               'reasoning_content': 'x' * 500}  # sub-classic threshold
        usage = {'_stream_anomaly': True, '_missing_done': True,
                 '_chunks_received': 10, 'stream_elapsed_ms': 120000,
                 'trace_id': 'M-EMPTY-ANOM'}
        d = analyse_stream_result(
            assistant_msg=msg, last_finish_reason='stop', task=task,
            tid='hard', model='kimi-k3', round_num=0,
            _premature_retry_count=0, messages=[], usage=usage)
        self.assertEqual(d['action'], 'break')
        self.assertEqual(d['last_finish_reason'], 'abnormal_stop')
        self.assertIsNotNone(task['error'])
        self.assertEqual(task['error'].get('kind'), 'abnormal_stop')


# ─────────────────────────────────────────────────────────────────────
#  G3 — slot truncation cooldown: rolling window beats interleaved successes
# ─────────────────────────────────────────────────────────────────────

class TestTruncationWindowCooldown(unittest.TestCase):

    def _slot(self):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name='k1', api_key='x', model='m1',
                    capabilities={'text'})

    def test_intermittent_truncations_cool_via_window(self):
        """success/truncate alternating never reaches 3 consecutive — the
        pre-fix slot NEVER cooled (measured: 19 closes on 2026-08-05, streak
        hit 3 only twice). The 10-min window catches it."""
        s = self._slot()
        for _ in range(2):
            s.record_success(latency_ms=100)
            s.record_truncation('premature stream close (no [DONE])')
            self.assertEqual(s.consecutive_errors, 1)  # streak reset each time
            self.assertEqual(s.cooldown_until, 0.0)    # not yet — 2 < 3
        s.record_success(latency_ms=100)
        s.record_truncation('premature stream close (no [DONE])')
        # Third truncation inside the window despite the reset streak.
        self.assertEqual(s.consecutive_errors, 1)
        self.assertGreater(s.cooldown_until, time.time())
        self.assertEqual(s.cooldown_reason, 'error')

    def test_single_truncation_does_not_cool(self):
        s = self._slot()
        s.record_truncation('one-off blip')
        self.assertEqual(s.cooldown_until, 0.0)

    def test_window_prunes_old_events(self):
        from lib.llm_dispatch import slot as slot_mod
        s = self._slot()
        stale = time.time() - slot_mod._TRUNCATION_WINDOW_S - 10
        s._truncation_events.append(stale)
        s._truncation_events.append(stale)
        s.record_truncation('fresh blip')  # prunes the two stale entries
        self.assertEqual(len(s._truncation_events), 1)
        self.assertEqual(s.cooldown_until, 0.0)


# ─────────────────────────────────────────────────────────────────────
#  G4 — async_proxy_for: async transport honours env no_proxy like the sync one
# ─────────────────────────────────────────────────────────────────────

class TestAsyncProxyFor(unittest.TestCase):

    _ENV_KEYS = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY',
                 'no_proxy', 'NO_PROXY')

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        os.environ['http_proxy'] = 'http://corp-proxy:8412'
        os.environ['https_proxy'] = 'http://corp-proxy:8412'
        os.environ['HTTP_PROXY'] = 'http://corp-proxy:8412'
        os.environ['HTTPS_PROXY'] = 'http://corp-proxy:8412'

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Fictional pair on purpose: the bypass host and the URL host must keep
    # their suffix relationship through the opensource export sanitizer — a
    # real internal pair (your-llm-gateway.example.com + example-corp.com) gets rewritten
    # INDEPENDENTLY (host → api.openai.com, suffix → example-corp.com) and
    # the exported test then fails deterministically on CI while passing
    # locally. 'corp-example.internal' survives every rewrite rule verbatim.
    _BYPASS = 'localhost,corp-example.internal'
    _INTERNAL_URL = 'https://llm-gw.corp-example.internal/v1/chat/completions'

    def test_env_no_proxy_suffix_bypasses(self):
        from lib.proxy import async_proxy_for
        # lib.proxy rebuilds os.environ['no_proxy'] from its IMPORT-TIME
        # baseline (_ENV_NO_PROXY) on every _sync_no_proxy() (Settings save /
        # startup config apply — the async boot thread can land one mid-test
        # under a loaded CI runner). A re-sync between the env set below and
        # the assertion would silently drop the suffix, so pin the baseline
        # to the same value: any mid-test re-sync rewrites what we set.
        import lib.proxy as _lp
        saved_baseline = _lp._ENV_NO_PROXY
        _lp._ENV_NO_PROXY = self._BYPASS
        try:
            os.environ['no_proxy'] = self._BYPASS
            self.assertIsNone(async_proxy_for(self._INTERNAL_URL))
        finally:
            _lp._ENV_NO_PROXY = saved_baseline

    def test_external_host_uses_proxy(self):
        from lib.proxy import async_proxy_for
        os.environ['no_proxy'] = self._BYPASS
        self.assertEqual(async_proxy_for('https://api.openai.com/v1/x'),
                         'http://corp-proxy:8412')

    def test_no_no_proxy_env_routes_internal_via_proxy(self):
        from lib.proxy import async_proxy_for
        os.environ.pop('no_proxy', None)
        os.environ.pop('NO_PROXY', None)
        self.assertEqual(async_proxy_for(self._INTERNAL_URL),
                         'http://corp-proxy:8412')

    def test_localhost_always_direct(self):
        from lib.proxy import async_proxy_for
        os.environ.pop('no_proxy', None)
        os.environ.pop('NO_PROXY', None)
        self.assertIsNone(async_proxy_for('http://127.0.0.1:15000/api'))
        self.assertIsNone(async_proxy_for('http://localhost:15000/api'))

    def test_registered_host_direct(self):
        from lib.proxy import async_proxy_for, register_no_proxy_host
        os.environ.pop('no_proxy', None)
        os.environ.pop('NO_PROXY', None)
        register_no_proxy_host('10.99.1.23')
        try:
            self.assertIsNone(async_proxy_for('http://10.99.1.23:8000/v1'))
        finally:
            from lib.proxy import _registered_hosts
            _registered_hosts.discard('10.99.1.23')


# ─────────────────────────────────────────────────────────────────────
#  Swarm SubAgent — poisoned rounds retry via the chassis, never execute
# ─────────────────────────────────────────────────────────────────────

def _mk_agent(dispatch_fn, events=None):
    from lib.swarm.agent import SubAgent
    from lib.swarm.types import SubTaskSpec
    spec = SubTaskSpec(role='coder', objective='truncation-guard test',
                       max_rounds=0, timeout_seconds=0)
    agent = SubAgent(
        spec,
        parent_task={},
        all_tools=[],
        model='trunc-model',
        thinking_enabled=False,
        on_event=events,
        abort_check=None,
        build_body_fn=lambda **kw: dict(kw),
        dispatch_stream_fn=dispatch_fn,
    )
    agent._tool_batches = []

    def _fake_exec(tool_calls, round_num):
        agent._tool_batches.append((round_num, list(tool_calls)))
        for tc in tool_calls:
            agent.messages.append({
                'role': 'tool', 'tool_call_id': tc.get('id', 'x'),
                'content': f'result:{tc["function"]["name"]}'})
    agent._execute_tool_calls = _fake_exec
    return agent


def _usage_missing_done():
    return {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2,
            '_missing_done': True, '_stream_anomaly': True,
            'trace_id': 'M-SWARM-TRUNC'}


def _usage_clean():
    return {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}


class TestSwarmPrematureCloseGuard(unittest.TestCase):

    def test_truncated_tool_call_round_retries_never_executes(self):
        """Round 1 dies mid-arguments → discarded before history append and
        retried via the chassis bonus; the corrupt call NEVER reaches the
        tool pool."""
        from lib.swarm.types import SubAgentStatus
        corrupt = {'role': 'assistant', 'content': '',
                   'tool_calls': [_tc('write_file', '{"path": "a", "content": "ab')]}
        final = {'role': 'assistant',
                 'content': 'final answer after the retry — substantive'}
        seq = [(corrupt, 'stop', _usage_missing_done()),
               (final, 'stop', _usage_clean())]
        disp = {'n': 0}

        def dispatch(body, **kw):
            m = seq[min(disp['n'], len(seq) - 1)]
            disp['n'] += 1
            return m

        agent = _mk_agent(dispatch)
        agent._run_loop(time.time())
        self.assertEqual(disp['n'], 2, 'one poisoned round + one retry')
        self.assertEqual(agent._tool_batches, [],
                         'corrupt tool call must never execute')
        # The poisoned assistant message was never appended to history.
        self.assertFalse(any(isinstance(m, dict) and m.get('tool_calls')
                             for m in agent.messages), agent.messages)
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertEqual(agent.result.final_answer, final['content'])
        self.assertEqual(agent._poison_strikes, 1)

    def test_empty_close_retries_then_degrades_without_loop(self):
        """An empty premature-close round retries up to the bonus cap, then
        the loop settles (no infinite re-issue)."""
        from lib.swarm.types import SubAgentStatus
        empty = {'role': 'assistant', 'content': ''}
        disp = {'n': 0}

        def dispatch(body, **kw):
            disp['n'] += 1
            return empty, 'stop', _usage_missing_done()

        agent = _mk_agent(dispatch)
        agent._run_loop(time.time())
        # 1 base round + 2 bonus rounds, then the exhausted fall-through.
        self.assertEqual(disp['n'], 3)
        self.assertEqual(agent._poison_strikes, 2)
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)

    def test_clean_rounds_untouched_by_guard(self):
        """No _missing_done → zero behavior change (the guard is inert)."""
        from lib.swarm.types import SubAgentStatus
        disp = {'n': 0}

        def dispatch(body, **kw):
            disp['n'] += 1
            return ({'role': 'assistant',
                     'content': 'a complete clean answer'}, 'stop',
                    _usage_clean())

        agent = _mk_agent(dispatch)
        agent._run_loop(time.time())
        self.assertEqual(disp['n'], 1)
        self.assertEqual(agent.result.status, SubAgentStatus.COMPLETED.value)
        self.assertFalse(hasattr(agent, '_poison_strikes'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
