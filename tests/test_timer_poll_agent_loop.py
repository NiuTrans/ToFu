"""tests/test_timer_poll_agent_loop.py — Timer poll mini-agent loop parity.

``poll_timer`` used to hand-roll its ``for agent_round in range(N)`` tool-calling
loop. It was migrated onto the shared ``lib.agent_loop.run_agent_loop`` primitive
(the ``dispatch`` / ``execute_tool`` / ``on_tool_round`` hooks + ``AbortSignal``
seam) so the round loop lives in ONE place. These tests pin the behaviour that
must survive that refactor:

  * a multi-round tool → result → decision poll produces the same
    ``tool_trace`` (one entry per tool), ``total_tokens`` (summed across
    rounds), ``poll_model`` (captured from ``usage['_dispatch']``), and
    parsed ``(ready, reason)`` decision;
  * tools remain available beyond the former poll cap until the model returns
    a natural decision;
  * a dispatch exception is caught and reported as an ``LLM error`` parse_error
    tuple, preserving tokens accrued so far.

DB-free: ``_get_timer_row`` / ``smart_chat`` / ``_execute_poll_tool`` are all
stubbed, so the loop runs purely in memory (no timer_watchers table needed).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import lib.scheduler.timer as timer_mod

pytestmark = pytest.mark.unit


def _active_timer():
    return {
        'id': 'tmr_test', 'status': 'active',
        'check_instruction': 'Is it done?', 'check_command': '',
        'tools_config': '{}', 'poll_count': 0,
    }


def _tc(name, args):
    return {'id': 'call_' + name, 'type': 'function',
            'function': {'name': name, 'arguments': args}}


@pytest.fixture(autouse=True)
def _stub_timer_row(monkeypatch):
    monkeypatch.setattr(timer_mod, '_get_timer_row', lambda tid: _active_timer())


def _install_smart_chat(monkeypatch, script):
    """Install a scripted smart_chat. ``script`` is a list of (content, usage)."""
    calls = {'n': 0}

    def _fake(messages, **kwargs):
        i = calls['n']
        calls['n'] += 1
        return script[min(i, len(script) - 1)]

    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _fake, raising=True)
    return calls


def test_multi_round_tool_then_decision(monkeypatch):
    """Round 0 calls a tool; round 1 returns the JSON decision."""
    script = [
        # Round 0: one tool call, tokens 10, model resolved.
        (None, {'total_tokens': 10, '_dispatch': {'model': 'cheap-x'},
                '_tool_calls': [_tc('read_files', '{"path":"a"}')]}),
        # Round 1: final decision, no tool calls, tokens 5.
        (json.dumps({'ready': True, 'reason': 'all green'}),
         {'total_tokens': 5}),
    ]
    _install_smart_chat(monkeypatch, script)

    executed = []

    def _fake_exec(tc, timer_id, project_path):
        executed.append(tc['function']['name'])
        return ('tool output', 0.12, False)

    monkeypatch.setattr(timer_mod, '_execute_poll_tool', _fake_exec)

    (ready, reason, tokens, skipped, parse_error, cmd_output,
     model, tool_trace, raw_content) = timer_mod.poll_timer('tmr_test')

    assert ready is True
    assert reason == 'all green'
    assert parse_error is False
    assert skipped is False
    assert tokens == 15, 'tokens must sum across both rounds'
    assert model == 'cheap-x', 'poll_model captured from usage[_dispatch]'
    assert executed == ['read_files']
    assert len(tool_trace) == 1
    assert tool_trace[0] == {
        # argsBrief is the name-keyed display label (format_tool_args_brief),
        # NOT the old raw-arguments truncation — the raw JSON string is coerced
        # and rendered through project_tool_display.
        'name': 'read_files', 'argsBrief': 'Read 1 file: a',
        'elapsed': 0.12, 'isError': False,
    }
    assert raw_content == json.dumps({'ready': True, 'reason': 'all green'})


def test_tools_continue_until_natural_decision(monkeypatch):
    """Tools stay available beyond the retired five-round poll cap."""
    always_tool = (None, {'total_tokens': 1,
                          '_tool_calls': [_tc('grep_search', '{"pattern":"x"}')]})
    final = (json.dumps({'ready': False, 'reason': 'not yet'}),
             {'total_tokens': 1})
    calls = _install_smart_chat(monkeypatch, [always_tool] * 8 + [final])

    monkeypatch.setattr(timer_mod, '_execute_poll_tool',
                        lambda tc, tid, pp: ('r', 0.01, False))

    result = timer_mod.poll_timer('tmr_test')
    tool_trace = result[7]

    assert calls['n'] == 9
    assert len(tool_trace) == 8


def test_dispatch_exception_reported(monkeypatch):
    """A smart_chat exception → LLM-error tuple, tokens preserved, parse_error."""
    def _boom(messages, **kwargs):
        raise RuntimeError('upstream 500')

    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _boom, raising=True)

    (ready, reason, tokens, skipped, parse_error, cmd_output,
     model, tool_trace, raw_content) = timer_mod.poll_timer('tmr_test')

    assert ready is False
    assert parse_error is True
    assert 'LLM error' in reason
    assert 'upstream 500' in reason
    assert tool_trace == []


def test_parse_failure_on_prose_decision(monkeypatch):
    """A non-JSON final content → parse_error with the raw text preserved."""
    _install_smart_chat(monkeypatch, [('not json at all', {'total_tokens': 3})])

    (ready, reason, tokens, skipped, parse_error, cmd_output,
     model, tool_trace, raw_content) = timer_mod.poll_timer('tmr_test')

    assert ready is False
    assert parse_error is True
    assert raw_content == 'not json at all'
    assert tokens == 3


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
