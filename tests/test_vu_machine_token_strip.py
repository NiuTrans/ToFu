"""tests/test_vu_machine_token_strip.py — VU machine-control token strip (pt_0ae59e94).

The VU protocol carries TWO machine-control tokens: ``[VU: TASK_DONE]`` and
``[PROGRESS: resolved=X remaining=Y]``.  Both must be stripped through ONE
predicate (``lib.agent_verdict.strip_machine_tokens``, backed by ONE
registry) before a VU reply is persisted into conversation history — a
leaked token is re-read by the next turn as ordinary user text and the
model starts authoring the signal itself (production: 90 lines across 52
conversations).

The budget guard (``_record_vu_turn_and_check_budget``) is the ONE consumer
that must still see the raw PROGRESS line (it drives the
diminishing-returns ledger), so the strip happens AFTER guard consumption
and BEFORE persistence — the guard reads the original, the DB stores the
clean copy.

Layers pinned here:
  1. predicate unit behaviour (strip / keep= / idempotence / validation);
  2. the registry as single source of truth (both tokens, and the PROGRESS
     pattern IS the verdict parser's own regex — no re-implementation);
  3. source scans: no hardcoded sentinel strip left in autopilot, and the
     deliberate ``keep=('progress_line',)`` asymmetry in run_virtual_user
     stays (a "helpful" full strip there would silently kill the budget
     guard's progress signal);
  4. behavioral: maybe_run_autopilot persists the CLEAN text while the
     budget guard receives the RAW text.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.tasks_pkg.autopilot as ap
from lib.agent_verdict import VU_DONE_SENTINEL, strip_machine_tokens
from lib.agent_verdict._handoff import _MACHINE_TOKEN_STRIP_PATTERNS

PROGRESS_SAMPLE = '[PROGRESS: resolved=2 remaining=3]'
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1. Predicate unit behaviour ───────────────────────────────────────────

def test_strip_removes_both_tokens_and_keeps_prose():
    text = '继续修第三条。\n' + VU_DONE_SENTINEL + '\n' + PROGRESS_SAMPLE
    out = strip_machine_tokens(text)
    assert VU_DONE_SENTINEL not in out
    assert '[PROGRESS' not in out
    assert '继续修第三条。' in out


def test_strip_idempotent_and_empty_safe():
    once = strip_machine_tokens('x\n' + PROGRESS_SAMPLE)
    assert strip_machine_tokens(once) == once
    assert strip_machine_tokens('') == ''
    assert strip_machine_tokens(None) is None


def test_strip_leaves_ordinary_text_untouched():
    text = '第一轮完成 2 项，剩 3 项。\n继续。'
    assert strip_machine_tokens(text) == text


def test_keep_progress_preserves_line_but_strips_done():
    text = 'go on\n' + VU_DONE_SENTINEL + '\n' + PROGRESS_SAMPLE
    out = strip_machine_tokens(text, keep=('progress_line',))
    assert VU_DONE_SENTINEL not in out
    assert PROGRESS_SAMPLE in out


def test_keep_unknown_label_raises():
    with pytest.raises(ValueError):
        strip_machine_tokens('x', keep=('not_a_token',))


# ── 2. Registry = single source of truth ──────────────────────────────────

def test_registry_covers_both_protocol_tokens():
    labels = {label for label, _ in _MACHINE_TOKEN_STRIP_PATTERNS}
    assert labels == {'vu_done_sentinel', 'progress_line'}
    pats = dict(_MACHINE_TOKEN_STRIP_PATTERNS)
    assert pats['vu_done_sentinel'].search(VU_DONE_SENTINEL)
    assert pats['progress_line'].search(PROGRESS_SAMPLE)


def test_registry_progress_pattern_is_the_verdict_parsers_own():
    # A re-implemented PROGRESS regex copy is exactly the hand-copy
    # divergence lib/agent_verdict exists to kill — the strip must match
    # what parse_progress matches, by construction (same object).
    from lib.agent_verdict import _PROGRESS_RE
    pats = dict(_MACHINE_TOKEN_STRIP_PATTERNS)
    assert pats['progress_line'] is _PROGRESS_RE


# ── 3. Source-scan guards ─────────────────────────────────────────────────

def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_autopilot_has_no_hardcoded_token_strip():
    src = _src('lib/tasks_pkg/autopilot.py')
    assert '.replace(_VU_DONE_SENTINEL' not in src
    assert 'strip_machine_tokens' in src


def test_run_virtual_user_keeps_progress_for_the_budget_guard():
    # The deliberate asymmetry: run_virtual_user strips the DONE sentinel
    # but KEEPS the PROGRESS line (the budget guard parses it); the
    # persistence path strips everything. Pin the keep= call so a
    # "helpful" full strip at that spot — which would starve the
    # diminishing-returns ledger of its signal — is caught by review-bot.
    src = _src('lib/tasks_pkg/autopilot.py')
    assert "keep=('progress_line',)" in src


# ── 4. Behavioral: persist clean, guard reads raw ─────────────────────────

@pytest.fixture
def vu_env(monkeypatch):
    captured = {}
    monkeypatch.setattr(ap, 'is_autopilot_enabled', lambda task: True)
    monkeypatch.setattr(ap, '_has_pending_real_message', lambda conv_id, *, user_id: False)
    monkeypatch.setattr(ap, '_successor_already_running',
                        lambda task, conv_id: False)
    monkeypatch.setattr(ap, '_get_or_persist_run_id',
                        lambda conv_id, *, user_id: 'ar-test000001')

    vu_text = '第三条还没修，继续。\n' + PROGRESS_SAMPLE
    monkeypatch.setattr(
        ap, 'run_virtual_user',
        lambda task, vu_msg_id=None: {'text': vu_text, 'rounds': [],
                                      'segments': []})

    def fake_append(task, conv_id, vu_msg_id, text, rounds=None, run_id='',
                    segments=None):
        captured['persist_text'] = text
        return {'_msgId': vu_msg_id, 'content': text, 'role': 'user'}
    monkeypatch.setattr(
        ap, '_append_conversation_autopilot_turns', fake_append)

    def fake_budget(conv_id, vu_text, *, user_id, targets=None):
        captured['budget_text'] = vu_text
        return {'stop': False, 'reason': '', 'turn': 1}
    monkeypatch.setattr(ap, '_record_vu_turn_and_check_budget', fake_budget)
    monkeypatch.setattr(ap, '_start_followup_task',
                        lambda task, conv_id: 'followup000001')

    import lib.tasks_pkg.manager as mgr
    monkeypatch.setattr(mgr, 'append_event', lambda task, ev: None)
    return captured


def test_persist_strips_machine_tokens_but_budget_guard_reads_raw(vu_env):
    task = {'id': 'task00000001', 'convId': 'conv00000001', '_userId': 1,
            'config': {},
            'modifiedFileList': []}
    out = ap.maybe_run_autopilot(task)
    assert out is not None
    assert out['next_task_id'] == 'followup000001'
    # The leak: nothing machine-flavoured reaches conversation history.
    assert '[PROGRESS' not in vu_env['persist_text']
    assert VU_DONE_SENTINEL not in vu_env['persist_text']
    assert '第三条还没修，继续。' in vu_env['persist_text']
    # The guard still gets the original PROGRESS line to parse.
    assert PROGRESS_SAMPLE in vu_env['budget_text']
