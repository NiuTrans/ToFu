"""tests/test_timer_parse_failure.py — Timer Watcher parse-failure diagnostics.

Covers the 2026-06-23 change that makes an unparseable poll decision
locatable and inspectable instead of a bare truncated reason:
  * ``poll_timer`` returns a 9-tuple whose last element is the LLM's FULL
    raw output, and flags ``parse_error=True`` when the decision is not JSON.
  * ``_record_poll`` persists the new ``poll_id`` + ``raw_output`` columns.
  * A clean (parseable) decision still returns ``parse_error=False`` and an
    empty/ignored raw dump path.

Durability assertions use the real Sidecar test runtime.
"""

import json

import pytest

import lib.scheduler.timer as timer_mod

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def _make_timer():
    """Create an active timer (no check_command → always calls the LLM)."""
    t = timer_mod.create_timer(
        user_id=1,
        conv_id='conv-parsefail',
        check_instruction='Is the run finished?',
        continuation_message='Summarize the results.',
        poll_interval=10,
        max_polls=120,
        check_command='',
        tools_config={},
        source_task_id='task-x',
    )
    return t['id']


def test_poll_timer_parse_failure_returns_raw(monkeypatch):
    """A non-JSON LLM reply → parse_error + the full raw text in slot 9."""
    timer_id = _make_timer()
    raw = 'From the JSON file, there are **29 genomes** and history length 29.'

    def _fake_smart_chat(messages, **kwargs):
        # No tool calls; content is prose, not JSON → parse must fail.
        return raw, {'total_tokens': 42, '_dispatch': {'model': 'deepseek-v4-flash-tencent'}}

    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _fake_smart_chat, raising=True)

    result = timer_mod.poll_timer(timer_id, user_id=1)
    assert len(result) == 9, 'poll_timer must return a 9-tuple (raw_content added)'
    ready, reason, tokens, skipped, parse_error, cmd_output, model, trace, raw_content = result

    assert ready is False
    assert skipped is False
    assert parse_error is True
    assert tokens == 42
    assert model == 'deepseek-v4-flash-tencent'
    assert raw_content == raw, 'raw_content must be the LLM output verbatim (untruncated)'
    assert 'See raw output below' in reason


def test_poll_timer_clean_decision_no_parse_error(monkeypatch):
    """A valid JSON decision → parse_error False and the decision honored."""
    timer_id = _make_timer()

    def _fake_smart_chat(messages, **kwargs):
        return json.dumps({'ready': True, 'reason': 'done'}), {'total_tokens': 7}

    import lib.llm_dispatch as _ld
    monkeypatch.setattr(_ld, 'smart_chat', _fake_smart_chat, raising=True)

    ready, reason, tokens, skipped, parse_error, cmd_output, model, trace, raw_content = \
        timer_mod.poll_timer(timer_id, user_id=1)
    assert ready is True
    assert parse_error is False
    assert reason == 'done'


def test_record_poll_persists_id_and_raw():
    """_record_poll writes poll_id + raw_output; get_timer_poll_log reads them back."""
    timer_id = _make_timer()
    poll_id = f'{timer_id}.p1'
    raw = 'unparseable model output here'

    timer_mod._record_poll(
        timer_id, 'parse_error', 'Could not parse the verification decision', 13,
        check_output='', model='deepseek-v4-flash-tencent',
        poll_id=poll_id, raw_output=raw, user_id=1,
    )

    log = timer_mod.get_timer_poll_log(timer_id, user_id=1, limit=5)
    assert log, 'poll log must have at least one row'
    row = log[0]
    assert row['poll_id'] == poll_id
    assert row['raw_output'] == raw
    assert row['decision'] == 'parse_error'
    assert timer_mod.get_timer(timer_id, user_id=1)['poll_count'] == 1


def test_record_poll_clean_omits_raw():
    """A clean wait/ready poll stores an empty raw_output (no needless dump)."""
    timer_id = _make_timer()
    timer_mod._record_poll(
        timer_id, 'wait', 'still running', 5,
        poll_id=f'{timer_id}.p1', raw_output='', user_id=1,
    )
    row = timer_mod.get_timer_poll_log(timer_id, user_id=1, limit=1)[0]
    assert row['raw_output'] == ''
    assert row['poll_id'] == f'{timer_id}.p1'
