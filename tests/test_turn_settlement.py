"""tests/test_turn_settlement.py — pin the turn-settlement verdict SSOT.

Drives ``lib/conversations/turn_settlement.compute_turn_settlement`` — the
single authoritative per-turn settlement fact that the interrupt bubble label,
the Continue-button affordance, and the resume-mode decision all READ instead
of re-inferring (design: docs/TURN_SETTLEMENT.md).

Covered faces:
  * outcome classification for every finishReason in the vocabulary;
  * the cause dimension (incl. the interruptedReason → cause mapping);
  * resume.mode precedence: completed→none, empty→regenerate,
    checkpoint→(lossy), prefill→(lossless), regenerate;
  * the manual-Stop lossless gap fix (P1b: 'aborted' resumes via prefill on a
    capable model);
  * faithfulness: a failed turn with a tool checkpoint still resumes via
    checkpoint (today's behaviour — the verdict is a faithful SSOT, not a
    re-litigation);
  * fail-closed degradation (unknown model / no content / unknown reason).
"""

import pytest

from lib.conversations.turn_settlement import (
    compute_turn_settlement,
    OUTCOME_COMPLETED, OUTCOME_INTERRUPTED, OUTCOME_TRUNCATED, OUTCOME_FAILED,
    CAUSE_MANUAL, CAUSE_KILLED, CAUSE_RESTART, CAUSE_UNKNOWN, CAUSE_OFFLINE, CAUSE_GATEWAY,
    CAUSE_MAX_TOKENS, CAUSE_TOOL_CAP, CAUSE_SAFETY_CAP, CAUSE_CONTENT_FILTER,
    CAUSE_ERROR,
    MODE_PREFILL, MODE_CHECKPOINT, MODE_REGENERATE, MODE_NONE,
)

pytestmark = pytest.mark.unit

CAPABLE = 'gpt-4o'               # model_supports_assistant_prefill → True
INCAPABLE = 'claude-sonnet-4-5'  # model_supports_assistant_prefill → False


def _amsg(content='', thinking='', finish_reason=None, tool_rounds=None,
          interrupted_reason=None, role='assistant'):
    m = {
        'role': role,
        'content': content,
        'thinking': thinking,
        'toolRounds': tool_rounds or [],
    }
    if finish_reason is not None:
        m['finishReason'] = finish_reason
    if interrupted_reason is not None:
        m['interruptedReason'] = interrupted_reason
    return m


def _done_round(call_id, name='read_files', content='result', llm_round=0):
    return {
        'toolCallId': call_id,
        'toolName': name,
        'status': 'done',
        'toolContent': content,
        'llmRound': llm_round,
        'assistantContent': 'prose before tool',
    }


# ─────────────────────────────────────────────────────────────────────────
#  Non-assistant / non-dict input → no verdict
# ─────────────────────────────────────────────────────────────────────────

def test_non_assistant_returns_none():
    assert compute_turn_settlement(None, model=CAPABLE) is None
    assert compute_turn_settlement({'role': 'user', 'content': 'hi'}, model=CAPABLE) is None
    assert compute_turn_settlement('not a dict', model=CAPABLE) is None


# ─────────────────────────────────────────────────────────────────────────
#  Outcome classification — the full finishReason vocabulary
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('fr', ['stop', 'end_turn', 'stop_sequence'])
def test_clean_finishes_are_completed(fr):
    v = compute_turn_settlement(_amsg(content='done answer', finish_reason=fr), model=CAPABLE)
    assert v['outcome'] == OUTCOME_COMPLETED
    assert v['cause'] is None
    assert v['finishReason'] == fr
    assert v['resume']['mode'] == MODE_NONE


@pytest.mark.parametrize('fr', ['length', 'max_tokens'])
def test_token_limit_is_truncated(fr):
    v = compute_turn_settlement(_amsg(content='partial', finish_reason=fr), model=CAPABLE)
    assert v['outcome'] == OUTCOME_TRUNCATED
    assert v['cause'] == CAUSE_MAX_TOKENS


def test_tool_cap_is_truncated():
    v = compute_turn_settlement(_amsg(content='x', finish_reason='tool_rounds_exhausted'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_TRUNCATED
    assert v['cause'] == CAUSE_TOOL_CAP


def test_incomplete_is_safety_cap_truncated():
    v = compute_turn_settlement(_amsg(content='x', finish_reason='incomplete'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_TRUNCATED
    assert v['cause'] == CAUSE_SAFETY_CAP


def test_content_filter_is_failed():
    v = compute_turn_settlement(_amsg(content='x', finish_reason='content_filter'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_FAILED
    assert v['cause'] == CAUSE_CONTENT_FILTER


@pytest.mark.parametrize('fr', ['error', 'abnormal_stop'])
def test_error_family_is_failed(fr):
    v = compute_turn_settlement(_amsg(content='x', finish_reason=fr), model=CAPABLE)
    assert v['outcome'] == OUTCOME_FAILED
    assert v['cause'] == CAUSE_ERROR


def test_interrupted_killed_maps_to_killed_cause():
    v = compute_turn_settlement(
        _amsg(content='x', finish_reason='interrupted', interrupted_reason='killed'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] == CAUSE_KILLED


def test_interrupted_manual_maps_to_restart_cause():
    v = compute_turn_settlement(
        _amsg(content='x', finish_reason='interrupted', interrupted_reason='manual'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] == CAUSE_RESTART


def test_interrupted_unknown_maps_to_unknown_cause():
    # Absent interruptedReason (legacy / first_boot) → UNKNOWN, NOT restart —
    # the bubble honestly shows "原因未知", so the verdict must not over-commit.
    v = compute_turn_settlement(_amsg(content='x', finish_reason='interrupted'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] == CAUSE_UNKNOWN


def test_server_offline_is_interrupted_offline():
    v = compute_turn_settlement(_amsg(content='x', finish_reason='server_offline'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] == CAUSE_OFFLINE


def test_premature_close_is_interrupted_gateway():
    v = compute_turn_settlement(_amsg(content='x', finish_reason='premature_close'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] == CAUSE_GATEWAY


def test_aborted_is_interrupted_manual():
    v = compute_turn_settlement(_amsg(content='x', finish_reason='aborted'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] == CAUSE_MANUAL


def test_missing_finish_reason_keeps_recovery_path_open():
    v = compute_turn_settlement(_amsg(content='x'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] is None
    assert v['finishReason'] is None


def test_unknown_finish_reason_keeps_recovery_path_open():
    v = compute_turn_settlement(_amsg(content='x', finish_reason='some_future_reason'), model=CAPABLE)
    assert v['outcome'] == OUTCOME_INTERRUPTED
    assert v['cause'] is None


# ─────────────────────────────────────────────────────────────────────────
#  Resume precedence
# ─────────────────────────────────────────────────────────────────────────

def test_empty_turn_regenerates():
    # No content, no thinking, no real tool round — nothing to resume.
    v = compute_turn_settlement(_amsg(finish_reason='interrupted'), model=CAPABLE)
    assert v['resume']['mode'] == MODE_REGENERATE
    assert v['resume']['reason'] == 'empty_turn'
    assert v['resume']['lossless'] is False


def test_completed_short_circuits_before_checkpoint():
    # A clean-stop turn with tool rounds has nothing to resume.
    v = compute_turn_settlement(
        _amsg(content='full answer', finish_reason='stop',
              tool_rounds=[_done_round('c1', llm_round=0)]),
        model=CAPABLE)
    assert v['resume']['mode'] == MODE_NONE


def test_capable_tools_turn_prefers_prefill_over_checkpoint():
    """THE P5 FLIP (owner-approved): a capable model with a resumable tail on a
    TOOLS turn reports prefill (lossless) — matching the case-2 wire the route
    ALREADY ships (replay the tool batch + prefill the tail). keptRounds is
    still surfaced (the route replays those rounds alongside the prefill)."""
    rounds = [_done_round('c1', llm_round=0), _done_round('c2', name='grep_search', llm_round=1)]
    v = compute_turn_settlement(
        _amsg(content='partial answer', finish_reason='interrupted', tool_rounds=rounds),
        model=CAPABLE)
    assert v['resume']['mode'] == MODE_PREFILL
    assert v['resume']['lossless'] is True
    assert v['resume']['keptRounds'] == 2   # surfaced, but the mode is prefill
    assert v['resume']['reason'] == 'prefill_continue'


def test_claude_tools_turn_falls_back_to_checkpoint():
    """The honest checkpoint fallback: a tools turn the provider can't prefill
    (Claude rejects a trailing assistant prefill) still resumes via checkpoint
    (replay the rounds, REGENERATE the tail — lossy, the only safe option)."""
    rounds = [_done_round('c1', llm_round=0), _done_round('c2', name='grep_search', llm_round=1)]
    v = compute_turn_settlement(
        _amsg(content='partial answer', finish_reason='interrupted', tool_rounds=rounds),
        model=INCAPABLE)
    assert v['resume']['mode'] == MODE_CHECKPOINT
    assert v['resume']['lossless'] is False
    assert v['resume']['keptRounds'] == 2
    assert v['resume']['reason'] == 'tool_checkpoint'


def test_failed_turn_with_checkpoint_still_resumes_via_checkpoint():
    # 'error' is NOT in RESUMABLE_FINISH_REASONS, so even a tools turn WITH a
    # tail cannot prefill — the honest fallback is checkpoint (replay the
    # completed rounds, regenerate). Faithful to the route (resume_prefill
    # returns None for a non-resumable finish reason).
    rounds = [_done_round('c1', llm_round=0)]
    v = compute_turn_settlement(
        _amsg(content='the partial answer', thinking='', finish_reason='error',
              tool_rounds=rounds),
        model=CAPABLE)
    assert v['resume']['mode'] == MODE_CHECKPOINT
    assert v['resume']['lossless'] is False


def test_prefill_is_lossless_for_capable_model_on_length():
    content = 'the answer so far'
    v = compute_turn_settlement(_amsg(content=content, finish_reason='length'), model=CAPABLE)
    assert v['resume']['mode'] == MODE_PREFILL
    assert v['resume']['lossless'] is True
    assert v['resume']['prefillChars'] == len(content)


def test_prefill_is_lossless_for_capable_model_on_interrupted():
    v = compute_turn_settlement(_amsg(content='partial prose', finish_reason='interrupted'), model=CAPABLE)
    assert v['resume']['mode'] == MODE_PREFILL
    assert v['resume']['lossless'] is True


def test_prefill_declined_for_claude_falls_back_to_regenerate():
    # The headline lossless gap: Claude rejects assistant prefill, so a
    # no-tools turn cut mid-prose honestly reports a full regeneration.
    v = compute_turn_settlement(_amsg(content='partial prose', finish_reason='length'), model=INCAPABLE)
    assert v['resume']['mode'] == MODE_REGENERATE
    assert v['resume']['lossless'] is False
    assert v['resume']['reason'] == 'no_checkpoint_no_prefill'


def test_error_without_checkpoint_regenerates():
    v = compute_turn_settlement(_amsg(content='partial', finish_reason='error'), model=CAPABLE)
    assert v['resume']['mode'] == MODE_REGENERATE


def test_missing_finish_reason_without_checkpoint_regenerates():
    # fr=None is not in RESUMABLE_FINISH_REASONS → prefill declined.
    v = compute_turn_settlement(_amsg(content='partial'), model=CAPABLE)
    assert v['resume']['mode'] == MODE_REGENERATE


def test_unknown_model_declines_prefill_fail_closed():
    # model=None → capability probe is skipped → no prefill.
    v = compute_turn_settlement(_amsg(content='partial', finish_reason='length'), model=None)
    assert v['resume']['mode'] == MODE_REGENERATE


# ─────────────────────────────────────────────────────────────────────────
#  P1b — the manual-Stop lossless gap fix
# ─────────────────────────────────────────────────────────────────────────

def test_manual_stop_with_content_resumes_via_lossless_prefill():
    """The single concrete lossless win: a user Stop (finishReason='aborted')
    on a no-tools turn with a prefill-capable model resumes via prefill —
    continuing the SAME partial prose — instead of a full regeneration that
    discards it. Fails before P1b ('aborted' not yet in
    RESUMABLE_FINISH_REASONS); passes after."""
    content = 'the partial answer the user stopped'
    v = compute_turn_settlement(_amsg(content=content, finish_reason='aborted'), model=CAPABLE)
    assert v['resume']['mode'] == MODE_PREFILL
    assert v['resume']['lossless'] is True
    assert v['resume']['prefillChars'] == len(content)


def test_manual_stop_claude_still_honest_regenerate():
    # 'aborted' is resumable, but Claude still declines prefill → honest regenerate.
    v = compute_turn_settlement(_amsg(content='partial', finish_reason='aborted'), model=INCAPABLE)
    assert v['resume']['mode'] == MODE_REGENERATE


def test_manual_stop_empty_turn_regenerates():
    # A Stop before any token has nothing to continue.
    v = compute_turn_settlement(_amsg(finish_reason='aborted'), model=CAPABLE)
    assert v['resume']['mode'] == MODE_REGENERATE
    assert v['resume']['reason'] == 'empty_turn'
