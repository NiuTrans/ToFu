"""Guards for the tool_calls-finish-WITHOUT-payload defense (2026-08-06 incident).

Incident replay (conv msh3qeplzneph5, task 153fccfc, round 3): the sankuai
gateway closed a kimi-k3 stream CLEANLY — [DONE] received, no transport
anomaly — reporting ``finish_reason=tool_calls``, yet not a single tool_call
delta was in the 61 chunks (~150 of 213 completion tokens vanished). Every
guard passed the round through:

  * none of the four raw-SSE anomaly classes (missing_done /
    missing_finish_reason / empty_stop / tool_name_unknown) covers
    "tool_calls finish with zero payload" — no raw frames were dumped;
  * ``analyse_stream_result`` normalized the lie to ``stop`` and its log
    blamed the phantom filter (which never fired — zero 'Filtering phantom'
    lines in the whole log);
  * the intent-stall nudge skipped (previous tool round had succeeded);
  * the suspicious-completion guard then delivered round-1's 89-char
    PREAMBLE ("我去查 402 链路…") as the turn's conclusion.

Two defense lines under test here:

  1. **Observation** — ``SSEAccumulator.finalize`` raises the fifth anomaly
     class ``tool_calls_no_payload`` (raw-frame dump + WARNING) and stamps
     ``usage['_tool_calls_void']`` distinguishing 'gateway_no_payload' (the
     wire carried no deltas) from 'filtered' (OUR phantom filter dropped
     every entry — its own WARNINGs then exist in the log).
  2. **Behaviour** — ``analyse_stream_result`` treats the shape like the
     other transport-lying buckets: transparent round retry (bounded,
     per-phase counter, content reset to round base), honest
     ``premature_close`` envelope on exhaustion — never a fake 'stop'.

NEUTER self-proof: deleting the finalize check fails group 1 (no dump call,
no usage stamp); reverting the analyse bucket to normalize-to-stop fails
group 2 (action='break' + last_finish_reason='stop', no retrying phase).

Run:  pytest tests/test_tool_calls_no_payload.py -v
      python tests/test_tool_calls_no_payload.py   (plain-assert subset)
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm._sse_core import SSEAccumulator  # noqa: E402
from lib.llm.diagnostics import RawSSEDumper  # noqa: E402
from lib.tasks_pkg.stream_handler import (  # noqa: E402
    _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
    analyse_stream_result,
)


# ─────────────────────────────────────────────────────────────────────
# 1) SSEAccumulator.finalize — the fifth anomaly class
# ─────────────────────────────────────────────────────────────────────

def _acc(model='kimi-k3', tools=None):
    body = {
        'model': model,
        'stream': True,
        'tools': tools if tools is not None else [
            {'type': 'function', 'function': {'name': 'grep_search'}},
        ],
    }
    dumper = RawSSEDumper(model, 'M-VOID-TEST', body)
    dumps = []
    # dump_anomaly is always-on and would write logs/raw_sse_anomaly.log —
    # record the call instead (the assertion target IS the call).
    dumper.dump_anomaly = lambda reason, **kw: dumps.append((reason, kw))
    acc = SSEAccumulator(body, 'M-VOID-TEST', dumper, None, time.time(),
                         log_prefix='[void-test]')
    return acc, dumps


def _feed(acc, delta, finish_reason=None, usage=None):
    chunk = {'choices': [{'delta': delta, 'finish_reason': finish_reason}]}
    if usage is not None:
        chunk['choices'][0]['usage'] = usage
    acc._process_openai_chunk(chunk)
    acc.chunk_count += 1


def test_tool_calls_finish_without_payload_dumps_anomaly():
    """Incident shape: thinking + 1-char content + clean finish_reason=
    tool_calls with ZERO tool_call deltas → dump + usage stamp."""
    acc, dumps = _acc()
    _feed(acc, {'reasoning_content': '查一下 402 链路…'})
    _feed(acc, {'content': '。'})
    _feed(acc, {}, finish_reason='tool_calls',
          usage={'prompt_tokens': 106691, 'completion_tokens': 213})
    acc.saw_done = True

    msg, finish_reason, usage = acc.finalize()

    assert finish_reason == 'tool_calls'
    assert 'tool_calls' not in msg
    assert [r for r, _ in dumps] == ['tool_calls_no_payload'], dumps
    reason, kw = dumps[0]
    assert kw['cause'] == 'gateway_no_payload'
    assert kw['pre_filter_count'] == 0
    assert usage.get('_tool_calls_void') == 'gateway_no_payload', usage


def test_filtered_cause_when_own_filter_dropped_every_entry():
    """The OTHER world: deltas DID arrive but our spurious-internal prefix
    filter dropped all of them → cause='filtered' (its drops are logged
    separately), so diagnosis never blames the gateway by default."""
    acc, dumps = _acc()
    _feed(acc, {'tool_calls': [
        {'index': 0, 'id': 'call_1', 'type': 'function',
         'function': {'name': '__internal_probe', 'arguments': '{}'}},
    ]})
    _feed(acc, {}, finish_reason='tool_calls',
          usage={'prompt_tokens': 10, 'completion_tokens': 5})
    acc.saw_done = True

    msg, finish_reason, usage = acc.finalize()

    assert 'tool_calls' not in msg  # every entry was filtered out
    assert [r for r, _ in dumps] == ['tool_calls_no_payload'], dumps
    assert dumps[0][1]['cause'] == 'filtered'
    assert dumps[0][1]['pre_filter_count'] == 1
    assert usage.get('_tool_calls_void') == 'filtered'


def test_tool_calls_finish_with_payload_is_silent():
    """Complement: a NORMAL tool_calls finish (payload present) raises no
    anomaly and carries no stamp."""
    acc, dumps = _acc()
    _feed(acc, {'tool_calls': [
        {'index': 0, 'id': 'call_1', 'type': 'function',
         'function': {'name': 'grep_search', 'arguments': ''}},
    ]})
    _feed(acc, {'tool_calls': [
        {'index': 0, 'function': {'arguments': '{"pattern":"402"}'}},
    ]})
    _feed(acc, {}, finish_reason='tool_calls',
          usage={'prompt_tokens': 10, 'completion_tokens': 5})
    acc.saw_done = True

    msg, finish_reason, usage = acc.finalize()

    assert finish_reason == 'tool_calls'
    assert len(msg.get('tool_calls') or []) == 1
    assert dumps == [], dumps
    assert '_tool_calls_void' not in usage


def test_stop_finish_with_content_is_silent():
    """Complement: an ordinary stop answer never trips the fifth class."""
    acc, dumps = _acc()
    _feed(acc, {'content': '完整答案。'})
    _feed(acc, {}, finish_reason='stop',
          usage={'prompt_tokens': 10, 'completion_tokens': 5})
    acc.saw_done = True

    _msg, finish_reason, usage = acc.finalize()

    assert finish_reason == 'stop'
    assert dumps == [], dumps
    assert '_tool_calls_void' not in usage


# ─────────────────────────────────────────────────────────────────────
# 2) analyse_stream_result — transparent retry, never a fake stop
# ─────────────────────────────────────────────────────────────────────

def _fresh_task(phase_counter: int = 0) -> dict:
    return {
        'id': 'void-test',
        'convId': 'conv-void',
        'aborted': False,
        'content': '',
        'thinking': '',
        'error': None,
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        '_premature_retry_count_phase': phase_counter,
    }


def _usage(cause='gateway_no_payload'):
    u = {
        '_stream_anomaly': False,
        '_empty_stop': False,
        '_chunks_received': 61,
        'trace_id': 'M-VOID-TEST',
        'stream_elapsed_ms': 128448,
        '_dispatch': {'key': 'sankuai_key_1', 'model': 'kimi-k3'},
    }
    if cause is not None:
        u['_tool_calls_void'] = cause
    return u


_WORK_TAIL = [
    {'role': 'user', 'content': '查一下 402 为什么没上面板。'},
    {'role': 'assistant', 'content': None,
     'tool_calls': [{'id': 'c1', 'type': 'function',
                     'function': {'name': 'grep_search', 'arguments': '{}'}}]},
    {'role': 'tool', 'tool_call_id': 'c1', 'content': '[grep results]'},
]

_INCIDENT_MSG = {'role': 'assistant', 'content': '。',
                 'reasoning_content': '先查 402 的完整链路…' * 20}


def test_incident_replay_retries_instead_of_fake_stop():
    """Epic acceptance: finish_reason=tool_calls + 0 assembled calls → the
    round MUST be retried (action='continue'), not normalized to 'stop'
    and delivered as a preamble."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg=dict(_INCIDENT_MSG),
        last_finish_reason='tool_calls',
        task=task, tid='void', model='kimi-k3',
        round_num=2, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'continue', decision
    assert decision['premature_retry_count'] == 1
    assert task['_premature_retry_count_phase'] == 1
    buckets = [e.get('bucket') for e in task['events']
               if isinstance(e, dict) and e.get('phase') == 'retrying']
    assert 'tool_calls_no_payload' in buckets, task['events']


def test_retry_resets_round_text_to_base():
    """The poisoned round's partial text/thinking is reset to the round base
    (with a discard-marked delta_reset), so the re-stream never stacks."""
    task = _fresh_task(phase_counter=0)
    task['content'] = '。'
    task['thinking'] = '一些思考'
    task['_round_base_content'] = 'BASE_C'
    task['_round_base_thinking'] = 'BASE_T'
    decision = analyse_stream_result(
        assistant_msg=dict(_INCIDENT_MSG),
        last_finish_reason='tool_calls',
        task=task, tid='void', model='kimi-k3',
        round_num=2, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'continue'
    assert task['content'] == 'BASE_C', task['content']
    assert task['thinking'] == 'BASE_T', task['thinking']
    resets = [e for e in task['events']
              if isinstance(e, dict) and e.get('type') == 'delta_reset']
    assert resets and resets[-1].get('discard') is True, task['events']
    residue = task.get('_floor_retry_residue') or []
    assert residue and residue[-1]['content'] == '。', residue


def test_retries_exhausted_settles_honest_premature_close():
    """At the cap: break with a premature_close error envelope — an honest
    terminal state (Continue stays available), never the fake 'stop' that
    delivered the incident's preamble."""
    task = _fresh_task(phase_counter=_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX)
    decision = analyse_stream_result(
        assistant_msg=dict(_INCIDENT_MSG),
        last_finish_reason='tool_calls',
        task=task, tid='void', model='kimi-k3',
        round_num=4, _premature_retry_count=_TOOL_CALLS_NO_PAYLOAD_RETRY_MAX,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'break', decision
    assert decision['last_finish_reason'] == 'premature_close', decision
    assert 'tool_calls_no_payload' in (decision['loop_exit_reason'] or '')
    assert task.get('error'), 'an honest error envelope is required'
    # Counter is NOT bumped past the cap.
    assert task['_premature_retry_count_phase'] == \
        _TOOL_CALLS_NO_PAYLOAD_RETRY_MAX


def test_unknown_cause_still_retries():
    """Older clients without the usage stamp: the wire finish_reason alone
    is enough — retry with cause='unknown' (never silently stop)."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg=dict(_INCIDENT_MSG),
        last_finish_reason='tool_calls',
        task=task, tid='void', model='kimi-k3',
        round_num=1, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(cause=None),
    )
    assert decision['action'] == 'continue', decision


def test_genuine_stop_with_content_unaffected():
    """Complement: a real stop answer (finish_reason=stop) after tool work
    is accepted normally — the bucket keys off the WIRE finish_reason."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant',
                       'content': '402 没上面板因为余额耗尽只记在 key 卡上……',
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=task, tid='void', model='kimi-k3',
        round_num=3, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(cause=None),
    )
    assert decision['action'] == 'break', decision
    assert decision['last_finish_reason'] == 'stop', decision
    assert decision['premature_retry_count'] == 0
    assert all(e.get('bucket') != 'tool_calls_no_payload'
               for e in task['events'] if isinstance(e, dict))


def test_real_tool_calls_present_unaffected():
    """Complement: finish_reason=tool_calls WITH assembled calls proceeds
    to tool execution exactly as before."""
    task = _fresh_task(phase_counter=0)
    msg = {'role': 'assistant', 'content': '',
           'tool_calls': [{'id': 'c9', 'type': 'function',
                           'function': {'name': 'grep_search',
                                        'arguments': '{}'}}]}
    decision = analyse_stream_result(
        assistant_msg=msg,
        last_finish_reason='tool_calls',
        task=task, tid='void', model='kimi-k3',
        round_num=1, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(cause=None),
    )
    assert decision['action'] == 'proceed', decision
    assert decision['premature_retry_count'] == 0


if __name__ == '__main__':
    import traceback
    failed = passed = 0
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            if 'monkeypatch' in (fn.__code__.co_varnames or ()):
                continue  # pytest-only fixtures
            try:
                fn()
                passed += 1
                print(f'PASS {name}')
            except Exception:
                failed += 1
                print(f'FAIL {name}')
                traceback.print_exc()
    print(f'\n{passed} passed, {failed} failed (monkeypatch tests skipped)')
    sys.exit(0 if failed == 0 else 1)
