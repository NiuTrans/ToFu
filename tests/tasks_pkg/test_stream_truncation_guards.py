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
  G2  a cut WITH partial content was soft-landed as a completed turn even
      though the gateway ended in the middle of an SSE JSON frame. Now the
      cut attempt's prose is discarded, ``phase:retrying`` tells the user
      what happened, and the round resends the unchanged request (no
      provider continues a bare trailing assistant message, and a
      continuation prompt would persist in context). Exhaustion is an
      honest error.
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

Run: pytest tests/tasks_pkg/test_stream_truncation_guards.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.agent_loop.agent_loop import unparseable_tool_calls  # noqa: E402
from lib.tasks_pkg.stream_handler.api import (  # noqa: E402
    RecoveryDecision,
    analyse_stream_result,
)
from lib.tasks_pkg.stream_handler._budget import (  # noqa: E402
    _PREMATURE_RETRY_MAX_CLASSIC,
)
from tests._registered_chat_task import registered_chat_task  # noqa: E402


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
    """Patch the retry-budget owner's backoff sleep for the test duration."""

    def __enter__(self):
        import lib.tasks_pkg.stream_handler._budget as sh
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
#  unparseable_tool_calls (shared helper, lib/agent_loop/agent_loop.py)
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
        with registered_chat_task(task), _no_sleep():
            d = self._analyse(task, msg, _missing_done_usage())
        self.assertIsInstance(d, RecoveryDecision)
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
        resets = [e for e in task['events']
                  if e.get('type') == EventType.DELTA_RESET]
        self.assertEqual(task.get('_contentEpoch'), 1)
        self.assertEqual(resets[-1].get('contentEpoch'), 1)
        phases = [e for e in task['events'] if e.get('type') == 'phase']
        self.assertTrue(phases and phases[-1].get('bucket')
                        == 'truncated_tool_args', task['events'])

    def test_valid_args_without_provider_completion_retry(self):
        """A parseable prefix cannot prove that the response has completed."""
        task = _fresh_task(content='work so far',
                           round_base_content='', round_base_thinking='')
        msg = {'role': 'assistant', 'content': 'some narration',
               'tool_calls': [_tc('read_files', '{"path": "x"}')]}
        with registered_chat_task(task), _no_sleep():
            d = self._analyse(task, msg, _missing_done_usage())
        self.assertEqual(d['action'], 'continue')
        self.assertIsNone(task['error'])
        self.assertTrue(any(event.get('bucket') == 'incomplete_tool_stream'
                            for event in task['events']))

    def test_malformed_frame_never_executes_even_parseable_tool_calls(self):
        """A dropped frame may contain an additional call, so a later clean
        finish cannot make the visible subset safe to execute."""
        task = _fresh_task(
            content='PRIOR untrusted preamble',
            round_base_content='PRIOR',
        )
        msg = {
            'role': 'assistant',
            'tool_calls': [_tc('read_files', '{"path":"safe.py"}')],
        }
        usage = {
            '_malformed_stream': True,
            '_malformed_frames': 1,
            '_stream_anomaly': True,
            '_stream_state': 'malformed_stream',
            '_chunks_received': 4,
            'trace_id': 'M-MALFORMED-TOOLS',
        }

        with registered_chat_task(task), _no_sleep():
            decision = self._analyse(task, msg, usage)

        self.assertEqual(decision['action'], 'continue')
        self.assertEqual(task['content'], 'PRIOR')
        phases = [event for event in task['events']
                  if event.get('type') == 'phase']
        self.assertEqual(phases[-1]['bucket'], 'malformed_tool_stream')

    def test_typed_malformed_state_is_authoritative_without_usage_flags(self):
        """The closed stream result, not the legacy usage bag, drives policy."""
        from lib.llm.stream_result import (
            ProviderStreamResult,
            ProviderStreamState,
        )

        task = _fresh_task(
            content='PRIOR untrusted preamble',
            round_base_content='PRIOR',
        )
        msg = {
            'role': 'assistant',
            'tool_calls': [_tc('read_files', '{"path":"safe.py"}')],
        }
        stream_result = ProviderStreamResult(
            message=msg,
            compatibility_finish_reason='stop',
            usage={},
            state=ProviderStreamState.MALFORMED_STREAM,
            malformed_frame_count=1,
        )

        with registered_chat_task(task), _no_sleep():
            decision = analyse_stream_result(
                assistant_msg=msg,
                last_finish_reason='stop',
                task=task,
                tid='typed-malformed',
                model='kimi-k3',
                round_num=1,
                _premature_retry_count=0,
                messages=[],
                usage={},
                stream_result=stream_result,
            )

        self.assertEqual(decision['action'], 'continue')
        self.assertEqual(decision.stream_state,
                         ProviderStreamState.MALFORMED_STREAM)
        self.assertEqual(task['content'], 'PRIOR')
        phases = [event for event in task['events']
                  if event.get('type') == 'phase']
        self.assertEqual(phases[-1]['bucket'], 'malformed_tool_stream')

    def test_typed_finished_state_clears_stale_legacy_anomaly_flags(self):
        """A stale usage bag cannot demote verified typed completion."""
        from lib.llm.stream_result import (
            ProviderStreamResult,
            ProviderStreamState,
        )

        task = _fresh_task(content='complete')
        msg = {'role': 'assistant', 'content': 'complete'}
        stream_result = ProviderStreamResult(
            message=msg,
            compatibility_finish_reason='stop',
            usage={
                '_stream_anomaly': True,
                '_missing_finish_reason': True,
                '_malformed_stream': True,
                '_malformed_frames': 3,
            },
            state=ProviderStreamState.PROVIDER_FINISHED,
            provider_finish_reason='stop',
            saw_finish_reason=True,
        )

        with registered_chat_task(task), _no_sleep():
            decision = analyse_stream_result(
                assistant_msg={'role': 'assistant', 'content': 'stale'},
                last_finish_reason='error',
                task=task,
                tid='typed-finished',
                model='kimi-k3',
                round_num=1,
                _premature_retry_count=0,
                messages=[],
                usage=stream_result.usage,
                stream_result=stream_result,
            )

        self.assertEqual(decision['action'], 'break')
        self.assertEqual(decision['loop_exit_reason'], 'no_tool_calls_round_1')
        self.assertEqual(decision['last_finish_reason'], 'stop')
        self.assertEqual(decision.stream_state,
                         ProviderStreamState.PROVIDER_FINISHED)
        self.assertIsNone(task['error'])
        self.assertEqual(task['events'], [])

    def test_typed_result_never_borrows_previous_round_usage(self):
        """The separate legacy usage argument may be a sticky prior round."""
        from lib.llm.stream_result import (
            ProviderStreamResult,
            ProviderStreamState,
        )

        task = _fresh_task(content='complete')
        msg = {'role': 'assistant', 'content': 'complete'}
        stream_result = ProviderStreamResult(
            message=msg,
            compatibility_finish_reason='stop',
            usage=None,
            state=ProviderStreamState.PROVIDER_FINISHED,
            provider_finish_reason='stop',
            saw_finish_reason=True,
        )

        decision = analyse_stream_result(
            assistant_msg=msg,
            last_finish_reason='stop',
            task=task,
            tid='typed-no-stale-usage',
            model='kimi-k3',
            round_num=1,
            _premature_retry_count=0,
            messages=[],
            usage={
                '_stream_anomaly': True,
                '_missing_finish_reason': True,
                '_dispatch': {'key': 'previous-round'},
            },
            stream_result=stream_result,
        )

        self.assertEqual(decision['action'], 'break')
        self.assertEqual(decision['loop_exit_reason'], 'no_tool_calls_round_1')
        self.assertEqual(decision['last_finish_reason'], 'stop')
        self.assertIsNone(task['error'])

    def test_typed_client_abort_stops_before_parseable_tool_call(self):
        from lib.llm.stream_result import (
            ProviderStreamResult,
            ProviderStreamState,
        )

        task = _fresh_task()
        msg = {
            'role': 'assistant',
            'tool_calls': [_tc('read_files', '{"path":"safe.py"}')],
        }
        stream_result = ProviderStreamResult(
            message=msg,
            compatibility_finish_reason='stop',
            usage={},
            state=ProviderStreamState.CLIENT_ABORTED,
        )

        decision = analyse_stream_result(
            assistant_msg=msg,
            last_finish_reason='stop',
            task=task,
            tid='typed-abort',
            model='kimi-k3',
            round_num=1,
            _premature_retry_count=0,
            messages=[],
            stream_result=stream_result,
        )

        self.assertEqual(decision['action'], 'break')
        self.assertEqual(decision['last_finish_reason'], 'aborted')
        self.assertEqual(decision['abort_detected_phase'],
                         'post_stream_round_1')

    def test_typed_unknown_stops_before_parseable_tool_call(self):
        from lib.llm.stream_result import (
            ProviderStreamResult,
            ProviderStreamState,
        )

        task = _fresh_task()
        msg = {
            'role': 'assistant',
            'tool_calls': [_tc('read_files', '{"path":"safe.py"}')],
        }
        stream_result = ProviderStreamResult(
            message=msg,
            compatibility_finish_reason='stop',
            usage={},
            state=ProviderStreamState.UNKNOWN,
        )

        decision = analyse_stream_result(
            assistant_msg=msg,
            last_finish_reason='stop',
            task=task,
            tid='typed-unknown',
            model='kimi-k3',
            round_num=1,
            _premature_retry_count=0,
            messages=[],
            stream_result=stream_result,
        )

        self.assertEqual(decision['action'], 'break')
        self.assertEqual(decision['last_finish_reason'], 'error')
        self.assertEqual(task['error']['kind'], 'internal')
        self.assertIn('unknown', decision['loop_exit_reason'])

    def test_missing_finish_evidence_retries_without_missing_done_flag(self):
        """An absent finish marker is sufficient even without a DONE hint."""
        task = _fresh_task()
        usage = _missing_done_usage()
        usage.pop('_missing_done')
        msg = {'role': 'assistant', 'content': '',
               'tool_calls': [_tc('write_file', '{"path":')]}
        with registered_chat_task(task), _no_sleep():
            d = self._analyse(task, msg, usage)
        self.assertEqual(d['action'], 'continue')

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
#  G2 — content-bearing stream anomaly: rewind + resend, never fake success
# ─────────────────────────────────────────────────────────────────────

class TestPartialContentRewindRetry(unittest.TestCase):

    @staticmethod
    def _usage():
        return {'_stream_anomaly': True, '_missing_done': True,
                '_chunks_received': 900, 'stream_elapsed_ms': 176000,
                'trace_id': 'M-PARTIAL'}

    def _cut(self, task, messages, *, model='kimi-k3', content='partial',
             retry_count=0, round_num=2):
        return analyse_stream_result(
            assistant_msg={'role': 'assistant', 'content': content,
                           'reasoning_content': 'cut reasoning'},
            last_finish_reason='stop', task=task, tid='partial', model=model,
            round_num=round_num, _premature_retry_count=retry_count,
            messages=messages, usage=self._usage())

    def test_partial_cut_rewinds_round_and_resends_unchanged_request(self):
        """The retry request is the interrupted one, byte for byte: nothing is
        appended to model context, the attempt's prose is discarded with an
        explicit epoch reset, and the retry is visible as a phase status."""
        from lib.agent_core.events import EventType
        task = _fresh_task(content='PRIOR partial answer body',
                           thinking='PRIOR-T cut reasoning',
                           round_base_content='PRIOR',
                           round_base_thinking='PRIOR-T')
        messages = [{'role': 'user', 'content': 'finish the diagnosis'}]
        sent = [dict(message) for message in messages]
        with registered_chat_task(task), _no_sleep():
            d = self._cut(task, messages, content=' partial answer body')

        self.assertEqual(d['action'], 'continue')
        self.assertEqual(d['premature_retry_count'], 1)
        self.assertEqual(messages, sent)
        self.assertEqual(task['content'], 'PRIOR')
        self.assertEqual(task['thinking'], 'PRIOR-T')
        self.assertEqual(task['_floor_retry_residue'][-1]['content'],
                         'PRIOR partial answer body')
        self.assertIsNone(task['error'])
        self.assertNotIn('_suppress_whole_turn_retry_to_preserve_partial', task)
        self.assertFalse(task.get('_injected_notes'))
        resets = [e for e in task['events']
                  if e.get('type') == EventType.DELTA_RESET]
        self.assertTrue(resets and resets[-1].get('discard'), task['events'])
        phase = [e for e in task['events'] if e.get('type') == 'phase'][-1]
        self.assertEqual(phase.get('phase'), 'retrying')
        self.assertEqual(phase.get('bucket'), 'partial_stream')
        self.assertEqual(phase.get('errorKind'), 'premature_close')
        self.assertNotIn('continuationMode', phase)
        self.assertEqual(phase.get('detailKey'),
                         'stream.phase.partialStreamRetry')
        self.assertEqual(phase.get('detailArgs'), {
            'chars': len(' partial answer body'), 'attempt': 1,
            'max': _PREMATURE_RETRY_MAX_CLASSIC})

    def test_rewind_does_not_depend_on_the_provider(self):
        """No provider continues a bare trailing assistant message, so every
        model family gets the same protocol-valid resend."""
        for model in ('claude-opus-5.5', 'gpt-6.1-sol', 'gemini-3.6-flash',
                      'kimi-k3', 'deepseek-v4-pro', 'glm-5.3'):
            with self.subTest(model=model):
                task = _fresh_task(content='cut tail', round_base_content='',
                                   round_base_thinking='')
                messages = [{'role': 'user', 'content': 'go'}]
                with registered_chat_task(task), _no_sleep():
                    d = self._cut(task, messages, model=model,
                                  content='cut tail')
                self.assertEqual(d['action'], 'continue')
                self.assertEqual(messages, [{'role': 'user', 'content': 'go'}])
                self.assertEqual(task['content'], '')

    def test_repeated_cuts_keep_resending_the_same_request(self):
        task = _fresh_task(content='first cut', round_base_content='',
                           round_base_thinking='')
        messages = [{'role': 'user', 'content': 'go'}]
        with registered_chat_task(task), _no_sleep():
            first = self._cut(task, messages, content='first cut')
            task['content'] = 'second, longer cut'
            second = self._cut(
                task, messages, content='second, longer cut',
                retry_count=first['premature_retry_count'])

        self.assertEqual(second['action'], 'continue')
        self.assertEqual(second['premature_retry_count'], 2)
        self.assertEqual(messages, [{'role': 'user', 'content': 'go'}])
        self.assertEqual(task['content'], '')

    def test_exhausted_budget_is_failed_but_keeps_last_partial_visible(self):
        prefix = 'still-visible partial answer'
        task = _fresh_task(phase_counter=_PREMATURE_RETRY_MAX_CLASSIC,
                           content=prefix, round_base_content='',
                           round_base_thinking='')
        messages = [{'role': 'user', 'content': 'go'}]
        d = self._cut(task, messages, content=prefix,
                      retry_count=_PREMATURE_RETRY_MAX_CLASSIC, round_num=3)

        self.assertEqual(d['action'], 'break')
        self.assertEqual(d['last_finish_reason'], 'premature_close')
        self.assertIn('retries_exhausted', d['loop_exit_reason'])
        self.assertEqual(task['error'].get('kind'), 'premature_close')
        self.assertEqual(task['content'], prefix)
        self.assertEqual(messages, [{'role': 'user', 'content': 'go'}])

    def test_no_content_anomaly_keeps_honest_error_after_retry_exhaustion(self):
        """An empty first-round cut retries, then keeps an honest error at cap."""
        task = _fresh_task(phase_counter=_PREMATURE_RETRY_MAX_CLASSIC)
        msg = {'role': 'assistant', 'content': '',
               'reasoning_content': 'x' * 500}  # sub-classic threshold
        usage = {'_stream_anomaly': True, '_missing_done': True,
                 '_chunks_received': 10, 'stream_elapsed_ms': 120000,
                 'trace_id': 'M-EMPTY-ANOM'}
        d = analyse_stream_result(
            assistant_msg=msg, last_finish_reason='stop', task=task,
            tid='hard', model='kimi-k3', round_num=0,
            _premature_retry_count=_PREMATURE_RETRY_MAX_CLASSIC,
            messages=[], usage=usage)
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
    # real internal pair (your-llm-gateway.example.com + internal.example.com) gets rewritten
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


def _usage_missing_done():
    return {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2,
            '_missing_done': True, '_stream_anomaly': True,
            'trace_id': 'M-SWARM-TRUNC'}


def _usage_clean():
    return {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}


if __name__ == '__main__':
    unittest.main(verbosity=2)
