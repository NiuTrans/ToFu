"""Guards for the canned-greeting upstream-artifact defense (pt_60b98556a2304b60).

Incident 2026-07-28 06:25→11:20: the example-corp gateway's only Opus 5 request-id
(``yuju-claude-opus-5-evaDaily``) intermittently answered ANY request with the
identical 29-char ``"Hi! How can I help you today?"`` + clean
``finish_reason=stop`` (real M-TraceIds). 68+ events / 14 conversations; both
the worker turns (26 assistant greetings) and the VU turns (18 synthetic
user-message greetings) were poisoned, and each VU greeting spawned a
follow-up task whose query WAS the greeting (amplification).

Two defense lines under test here (the third — persist-layer narration
preservation — is the sibling epic pt_473309109ace4240):

  1. **Stream layer** — ``analyse_stream_result`` detects the incongruent
     greeting and retries (bounded, per-phase counter) instead of accepting
     it as the turn's answer.
  2. **VU layer** — ``run_virtual_user`` refuses to relay the artifact into
     the conversation as a synthetic user turn.
  3. **Complement (no false positives)** — a greeting that IS congruent (the
     user really said "你好" / "在吗") is never retried, and normal answers
     never match the detector.

Run:  pytest tests/tasks_pkg/test_canned_greeting_retry.py -v
      python tests/tasks_pkg/test_canned_greeting_retry.py   (plain-assert subset)
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.tasks_pkg.stream_handler.api import analyse_stream_result  # noqa: E402
from lib.tasks_pkg.stream_handler._budget import _CANNED_GREETING_RETRY_MAX  # noqa: E402
from lib.tasks_pkg.stream_handler._canned_greeting import (  # noqa: E402
    is_canned_greeting_reply,
    last_user_is_smalltalk,
)

INCIDENT_TEXT = 'Hi! How can I help you today?'


@pytest.fixture(autouse=True)
def clean_synthetic_tasks():
    """Register classifier tasks with the event authority and clean them up."""
    yield
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        for task_id in [
            task_id for task_id in tasks if task_id.startswith('canned-test')
        ]:
            tasks.pop(task_id, None)
_WORK_TAIL = [
    {'role': 'user', 'content': '查一下明天去曼谷的机票，给出最便宜的三班。'},
    {'role': 'assistant', 'content': None,
     'tool_calls': [{'id': 'c1', 'type': 'function',
                     'function': {'name': 'searchFlights', 'arguments': '{}'}}]},
    {'role': 'tool', 'tool_call_id': 'c1', 'content': '[195KB of flights]'},
]
_KICKOFF_TAIL = [
    {'role': 'user',
     'content': '[Project Brain — autonomous dispatch] You are picking up an '
                'open project epic so sibling conversations do not redo it…'},
]


# ─────────────────────────────────────────────────────────────────────
# 1) Detector — pure verdict
# ─────────────────────────────────────────────────────────────────────

def test_incident_string_is_canned():
    """The exact 2026-07-28 artifact, answering real tool work → canned."""
    assert is_canned_greeting_reply(INCIDENT_TEXT, _WORK_TAIL) is True


def test_zh_variant_is_canned():
    """The zh opener family (auto-translated display form) is also caught."""
    assert is_canned_greeting_reply('你好！今天我能为您做些什么？', _WORK_TAIL) is True


def test_hello_may_i_assist_variant_is_canned():
    assert is_canned_greeting_reply('Hello! How may I assist you today?',
                                    _WORK_TAIL) is True


def test_kickoff_tail_is_canned():
    """A greeting answering a brain kickoff is an artifact (the R1 shape)."""
    assert is_canned_greeting_reply(INCIDENT_TEXT, _KICKOFF_TAIL) is True


def test_greeting_reply_to_user_nihao_not_canned():
    """Complement: user really said 你好 → greeting is legitimate, no retry."""
    msgs = [{'role': 'user', 'content': '你好'}]
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is False
    assert is_canned_greeting_reply('你好！今天我能为您做些什么？', msgs) is False


def test_greeting_reply_to_user_hey_not_canned():
    msgs = [{'role': 'user', 'content': 'hey'}]
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is False


def test_greeting_reply_to_user_zaima_not_canned():
    msgs = [{'role': 'user', 'content': '在吗'}]
    assert is_canned_greeting_reply('你好！今天我能为您做些什么？', msgs) is False


def test_question_mark_user_still_canned():
    """The ms40kfqq shape: user typed '?' after the first greeting and the
    upstream greeted AGAIN — '?' is not a greeting invitation, so retry."""
    msgs = [{'role': 'user', 'content': '?'}]
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is True


def test_short_non_greeting_not_canned():
    assert is_canned_greeting_reply('好的。', _WORK_TAIL) is False


def test_youre_welcome_not_canned():
    assert is_canned_greeting_reply("You're welcome!", _WORK_TAIL) is False


def test_long_answer_starting_with_greeting_not_canned():
    """A real answer that merely OPENS with a greeting is >60 chars → out."""
    long_answer = INCIDENT_TEXT + ' Here is the full report you asked for…'
    assert len(long_answer) > 60
    assert is_canned_greeting_reply(long_answer, _WORK_TAIL) is False


def test_block_form_user_smalltalk_complement():
    """Anthropic-style content blocks: user said hi in block form → legitimate."""
    msgs = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'hi'},
        {'type': 'image', 'source': {}},
    ]}]
    assert last_user_is_smalltalk(msgs) is True
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is False


def test_tool_result_user_block_not_smalltalk():
    """A tool-result user turn (Anthropic shape) is NOT small-talk → retry."""
    msgs = [
        {'role': 'user', 'content': 'Build the exporter.'},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'x', 'content': 'ok'},
        ]},
    ]
    assert last_user_is_smalltalk(msgs) is False
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is True


def test_empty_messages_fail_safe_no_retry():
    """Incongruence unprovable without messages → fail safe (no retry)."""
    assert is_canned_greeting_reply(INCIDENT_TEXT, []) is False


def test_no_user_turn_at_all_is_canned():
    """Nothing was asked → a greeting is definitionally an artifact."""
    msgs = [{'role': 'assistant', 'content': 'previous answer'}]
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is True


# ─────────────────────────────────────────────────────────────────────
# 2) analyse_stream_result — retry behavior (incident replay)
# ─────────────────────────────────────────────────────────────────────

def _fresh_task(phase_counter: int = 0) -> dict:
    task = {
        'id': 'canned-test',
        'convId': 'conv-canned',
        '_userId': 1,
        'aborted': False,
        'content': '',
        'thinking': '',
        'error': None,
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        '_premature_retry_count_phase': phase_counter,
    }
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        tasks[task['id']] = task
    return task


def _usage():
    return {
        '_stream_anomaly': False,
        '_empty_stop': False,
        '_chunks_received': 8,
        'trace_id': 'M-CANNED-TEST',
        'stream_elapsed_ms': 13400,
        '_dispatch': {'key': 'example_corp_key_2',
                      'model': 'yuju-claude-opus-5-evaDaily'},
    }


def _greeting_msg():
    return {'role': 'assistant', 'content': INCIDENT_TEXT,
            'reasoning_content': ''}


def test_canned_greeting_after_tool_round_retries():
    """Epic acceptance: replay the 09:51–10:45 shape (tool_calls history →
    stop + ≤50-char canned text) → the round MUST be retried, not accepted."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg=_greeting_msg(),
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=1, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1
    assert task['_premature_retry_count_phase'] == 1
    buckets = [e.get('bucket') for e in task['events']
               if isinstance(e, dict) and e.get('phase') == 'retrying']
    assert 'canned_greeting' in buckets, task['events']


def test_canned_greeting_on_round0_after_kickoff_retries():
    """The R1 shape (brain kickoff answered with a greeting) also retries."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg=_greeting_msg(),
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=0, _premature_retry_count=0,
        messages=list(_KICKOFF_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'continue'


def test_canned_greeting_retries_exhausted_accepts_without_fabricated_error():
    """At the cap, the response is ACCEPTED (never an invented error) —
    loud + audited, and the persist-layer interception preserves tool work."""
    task = _fresh_task(phase_counter=_CANNED_GREETING_RETRY_MAX)
    decision = analyse_stream_result(
        assistant_msg=_greeting_msg(),
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=3, _premature_retry_count=_CANNED_GREETING_RETRY_MAX,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'stop'
    assert task.get('error') in (None, '')
    # The counter is NOT bumped past the cap.
    assert task['_premature_retry_count_phase'] == _CANNED_GREETING_RETRY_MAX


def test_congruent_greeting_never_retried():
    """Epic complement: the user really said 你好 → no retry, normal stop."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant',
                       'content': '你好！今天我能为您做些什么？',
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=0, _premature_retry_count=0,
        messages=[{'role': 'user', 'content': '你好'}], usage=_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['premature_retry_count'] == 0
    assert all(e.get('bucket') != 'canned_greeting'
               for e in task['events'] if isinstance(e, dict))


def test_real_answer_never_retried():
    """A substantive stop answer never trips the detector."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant',
                       'content': '三班最便宜的是：1) CA959 ……（完整答案）' * 4,
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=4, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['premature_retry_count'] == 0


def test_short_non_greeting_stop_not_retried():
    """A genuine short answer ('好的。') after tool work is accepted — the
    detector matches the greeting FAMILY, not shortness itself."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant', 'content': '好的。',
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=2, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['premature_retry_count'] == 0


# ─────────────────────────────────────────────────────────────────────
# 2b) The 2026-08-02 deepseek-v4-flash shape: the per-turn tail injection
#     (managed Context Composer blocks — date / digest / charter / board) append
#     <system-reminder> blocks onto the LAST user message before the round,
#     so the production wire form of "你好" is a blocks list carrying a
#     reminder. The small-talk complement must judge the USER'S OWN WORDS,
#     not the system-injected padding — otherwise every laconic greeting
#     reply is misclassified as the upstream artifact and burned 2 retries.
# ─────────────────────────────────────────────────────────────────────

_DATE_REMINDER = '<system-reminder>\nCurrent date: 2026-08-02\n</system-reminder>'


def test_tail_reminder_block_form_smalltalk_complement():
    """Production wire shape: '你好' + appended date reminder block → the
    user's own words are still pure small-talk → greeting stays legitimate."""
    msgs = [{'role': 'user', 'content': [
        {'type': 'text', 'text': '你好'},
        {'type': 'text', 'text': _DATE_REMINDER},
    ]}]
    assert last_user_is_smalltalk(msgs) is True
    assert is_canned_greeting_reply('你好！有什么可以帮你的吗？', msgs) is False


def test_tail_reminder_string_form_smalltalk_complement():
    """Same protection when the reminder rode a string-form user message."""
    msgs = [{'role': 'user', 'content': '你好\n\n' + _DATE_REMINDER}]
    assert last_user_is_smalltalk(msgs) is True


def test_congruent_greeting_with_tail_reminder_never_retried():
    """Analyse-level replay of the incident: user said 你好 (tail-mutated),
    model answered a laconic greeting → NOT a canned artifact, no retry."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg={'role': 'assistant',
                       'content': '你好！有什么可以帮你的吗？',
                       'reasoning_content': ''},
        last_finish_reason='stop',
        task=task, tid='canned', model='deepseek-v4-flash',
        round_num=0, _premature_retry_count=0,
        messages=[{'role': 'user', 'content': [
            {'type': 'text', 'text': '你好'},
            {'type': 'text', 'text': _DATE_REMINDER},
        ]}], usage=_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['premature_retry_count'] == 0
    assert all(e.get('bucket') != 'canned_greeting'
               for e in task['events'] if isinstance(e, dict))


# ─────────────────────────────────────────────────────────────────────
# 2c) The 2026-09-08 false positive: Context Composer managed blocks are
#     wrapped in <!-- tofu-context:ID:start/end --> envelopes and ride as
#     a SEPARATE trailing user message (``_append_tail``). The markers
#     survive <system-reminder> stripping, so a bare "hi" turn misread as
#     non-small-talk and its legitimate 30-char greeting was retried (2/2).
# ─────────────────────────────────────────────────────────────────────

def _composer_tail(*block_ids):
    """A managed tail carrier in the production envelope shape."""
    return {'role': 'user', '_isMeta': True, '_contextComposer': True,
            'content': [{'type': 'text', 'text': (
                f'<!-- tofu-context:{bid}:start -->\n'
                '<system-reminder>\n'
                '[Context authority: ambient; source: test]\n'
                'payload\n'
                '</system-reminder>\n'
                f'<!-- tofu-context:{bid}:end -->')} for bid in block_ids]}


def test_composer_tail_after_hi_not_canned():
    """The screenshot shape: user said 'hi', the composer appended its
    managed tail as a separate user message → greeting stays legitimate."""
    msgs = [{'role': 'user', 'content': 'hi'},
            _composer_tail('current_date', 'relevant_memories')]
    assert last_user_is_smalltalk(msgs) is True
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is False


def test_composer_tail_does_not_shield_real_work():
    """The carrier is no small-talk alibi: a greeting answering real tool
    work behind a composer tail is still the artifact → retry."""
    msgs = [*_WORK_TAIL, _composer_tail('current_date')]
    assert last_user_is_smalltalk(msgs) is False
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is True


def test_project_context_envelope_without_reminder_not_canned():
    """project_context envelopes carry raw text (no <system-reminder>
    inside); the whole envelope must still be discounted."""
    msgs = [{'role': 'user', 'content': '在吗'},
            {'role': 'user', '_contextComposer': True, 'content': [
                {'type': 'text', 'text': (
                    '<!-- tofu-context:project_context:start -->\n'
                    '[PROJECT CO-PILOT MODE]\n'
                    'lots of project prose\n'
                    '<!-- tofu-context:project_context:end -->')}]}]
    assert last_user_is_smalltalk(msgs) is True
    assert is_canned_greeting_reply('你好！今天我能为您做些什么？', msgs) is False


def test_sanitized_wire_copy_without_flags_not_canned():
    """Provider-neutral sanitization strips private fields on the wire
    copy; envelope-stripping alone must still skip the carrier."""
    msgs = [{'role': 'user', 'content': 'hey'},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': (
                    '<!-- tofu-context:current_date:start -->\n'
                    '<system-reminder>\nCurrent date: 2026-09-08\n'
                    '</system-reminder>\n'
                    '<!-- tofu-context:current_date:end -->')}]}]
    assert last_user_is_smalltalk(msgs) is True
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is False


def test_only_carriers_no_user_words_still_canned():
    """Carriers alone are not a user turn: greeting stays an artifact."""
    msgs = [_composer_tail('current_date')]
    assert last_user_is_smalltalk(msgs) is False
    assert is_canned_greeting_reply(INCIDENT_TEXT, msgs) is True


def test_congruent_greeting_with_composer_tail_never_retried():
    """Analyse-level replay of the screenshot: 'hi' + managed tail carrier,
    model answered a 30-char greeting → NOT a canned artifact, no retry."""
    task = _fresh_task(phase_counter=0)
    decision = analyse_stream_result(
        assistant_msg=_greeting_msg(),
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=0, _premature_retry_count=0,
        messages=[{'role': 'user', 'content': 'hi'},
                  _composer_tail('relevant_memories', 'current_date')],
        usage=_usage(),
    )
    assert decision['action'] == 'break'
    assert decision['premature_retry_count'] == 0
    assert all(e.get('bucket') != 'canned_greeting'
               for e in task['events'] if isinstance(e, dict))


def test_canned_greeting_retry_resets_round_text():
    """The canned bucket is the ONLY retry bucket whose discarded round HAS
    content — the retry MUST drop the poisoned text (backend accumulators +
    a client delta_reset marked discard) or each attempt's greeting
    concatenates onto the last (the 2026-08-02 triple-greeting bug)."""
    task = _fresh_task(phase_counter=0)
    task['content'] = '你好！有什么可以帮你的吗？'
    task['thinking'] = '一点思考'
    decision = analyse_stream_result(
        assistant_msg=_greeting_msg(),
        last_finish_reason='stop',
        task=task, tid='canned', model='claude-opus-5',
        round_num=1, _premature_retry_count=0,
        messages=list(_WORK_TAIL), usage=_usage(),
    )
    assert decision['action'] == 'continue'
    assert task['content'] == '', task['content']
    assert task['thinking'] == '', task['thinking']
    resets = [e for e in task['events']
              if isinstance(e, dict) and e.get('type') == 'delta_reset']
    assert resets and resets[-1].get('discard') is True, task['events']
    assert task.get('_contentEpoch') == 1, task
    assert resets[-1].get('contentEpoch') == 1, resets[-1]
