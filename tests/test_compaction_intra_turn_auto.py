"""tests/test_compaction_intra_turn_auto.py — the AUTOMATIC-path fixes for the
single-giant-turn overflow, plus the tool-pairing safety of the last-resort
head-truncate net.

Three mechanisms under test, each with a load-bearing NEUTER control:

  #1  AUTOMATIC intra-turn fold — ``execute_compact_tool`` must fold the COLD
      tool-call rounds out of an in-flight giant turn that ``_find_turn_boundary``
      preserves WHOLE, so the automatic L2 path can actually shrink it (the gap
      the manual /compact 档B fold fixed only for the button, never for the
      per-round pipeline).  NEUTER: with the fold disabled the giant turn
      survives whole and tokens barely move.

  #2  HEAD-TRUNCATE tool-pairing — the emergency net drops whole
      ``assistant(tool_calls)+tool`` rounds as a unit and prunes any orphan
      ``tool`` result, so it can NEVER leave a ``tool`` message without its
      ``assistant.tool_calls`` parent (the exact HTTP-400 it exists to avert).
      NEUTER: a naive per-message pop that stops mid-round strands the results.

  #3  SHARED policy — the manual and automatic paths cut cold-vs-hot at the SAME
      boundary via ``_split_cold_rounds`` (one sanctioned constant, two index
      spaces), so the two compaction paths can't drift.

Run:  python -B -m pytest -p no:napari tests/test_compaction_intra_turn_auto.py
"""
from __future__ import annotations

import json
import logging
import os
import random
import string
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.tasks_pkg.compaction._layer2._compact as l2


# ── api-form builders ──────────────────────────────────────────────────────

def _sys():
    return {'role': 'system', 'content': 'you are a coding assistant'}


def _user(text):
    return {'role': 'user', 'content': text}


def _dense_chars(seed, n):
    """Deterministic high-entropy filler. Repetitive filler ('x'*n) BPE-merges
    into far fewer tokens than the char-based heuristic assumes, so the
    authoritative (tiktoken) and heuristic yardsticks disagree — the
    convergence tests need both to agree that the hot tail is over the
    ceiling. Random alnum keeps them aligned."""
    rng = random.Random(seed)
    return ''.join(rng.choices(string.ascii_letters + string.digits, k=n))


def _round(i, chars=4000, dense=False):
    """One api-form tool-call ROUND: assistant(tool_calls) + its tool result."""
    tcid = f'tc_{i}'
    payload = _dense_chars(i, chars) if dense else ('x' * chars)
    return [
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': tcid, 'type': 'function',
                         'function': {'name': 'read_files',
                                      'arguments': '{"path": "x"}'}}]},
        {'role': 'tool', 'tool_call_id': tcid, 'name': 'read_files',
         'content': 'RESULT ' + payload},
    ]


def _giant_turn_api(n_rounds=40, chars=4000, dense=False):
    """system + user(objective) + ONE turn of n_rounds tool-call rounds (no
    intervening user), i.e. a single agentic turn that fills the window."""
    msgs = [_sys(), _user('修复登录 bug，尽可能彻底')]
    for i in range(n_rounds):
        msgs += _round(i, chars=chars, dense=dense)
    return msgs


def _api_pairs_ok(msgs):
    """True iff every ``tool`` result has a preceding open ``tool_call`` id and
    no ``tool_call`` is left unmatched-forever (orphan detection)."""
    open_ids = set()
    for m in msgs:
        if m.get('role') == 'assistant' and m.get('tool_calls'):
            for tc in m['tool_calls']:
                open_ids.add(tc['id'])
        elif m.get('role') == 'tool':
            tcid = m.get('tool_call_id')
            if tcid not in open_ids:
                return False, f'orphan tool result {tcid}'
            open_ids.discard(tcid)
    return True, ''


@pytest.fixture
def stub_summary(monkeypatch):
    """Deterministic, hermetic summary + no archive side effects."""
    def _fake(
        old_messages,
        current_query,
        log_prefix='',
        conv_id='',
        task=None,
        usage_out=None,
        anchor_text='',
    ):
        return '### 1. Primary Request\n[folded earlier tool rounds summarized]'
    monkeypatch.setattr(l2, '_generate_query_aware_summary', _fake)
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)
    return _fake


def test_summary_dispatch_receives_durable_goal_separately_from_login_steer(
    monkeypatch,
):
    """Incident regression: the anchor reaches the model as verbatim evidence.

    mtbb5cqdk6itfp's first receipt named ``Unable to log in?`` as its entire
    objective because the verbatim anchor had been removed from the summary
    input with no evidence re-supplied. The anchor is now re-supplied as
    verbatim evidence while the model authors the Objective itself. Pin the
    dispatch boundary, not model behavior.
    """
    original_goal = (
        'Download the latest HOPE and LLM skills, then use their capabilities '
        'and npm CLIs to improve both corresponding MCP tools.')
    messages = [
        _sys(),
        _user(original_goal),
        {'role': 'assistant', 'content': 'download investigation ' + 'x' * 8_000},
        {'role': 'user', 'content': 'Unable to log in?',
         '_isInboxInject': True, '_containsHumanSteer': True},
        {'role': 'assistant', 'content': 'checking a non-login path'},
    ]
    captured = {}

    def summarize(old_messages, current_query, _prefix='', **kwargs):
        captured['old_messages'] = old_messages
        captured['current_query'] = current_query
        captured['anchor_text'] = kwargs['anchor_text']
        return '### Objective\nContinue the two-skill MCP improvement audit.'

    monkeypatch.setattr(l2, '_generate_query_aware_summary', summarize)
    monkeypatch.setattr(l2, '_archive_transcript', lambda *args, **kwargs: None)

    l2.execute_compact_tool(
        messages,
        task={'id': 'incident', 'convId': 'mtbb5cqdk6itfp',
              '_userId': 1, 'config': {}},
        preserve_budget_tokens=1,
        _compaction_skip_archive=True,
    )

    assert captured['anchor_text'] == original_goal
    assert captured['current_query'] == 'Unable to log in?'
    assert all(message.get('content') != original_goal
               for message in captured['old_messages'])
    assert any(message.get('role') == 'user'
               and message.get('content') == original_goal
               for message in messages)


def test_compact_repins_autopilot_objective_from_receipt(monkeypatch):
    """Goal-replacement wiring: an accepted receipt whose model-authored
    Objective differs from the autopilot pin re-pins it. The re-pin helper is
    fail-safe and never mints a pin for non-autopilot conversations; this
    pins only that compaction HANDS the receipt's Objective over."""
    import lib.tasks_pkg.autopilot_state as ap_state
    original_goal = 'Build the UTF-8 CSV exporter.'
    new_goal = 'Rewrite the report as a press release.'
    messages = [
        _sys(),
        _user(original_goal),
        {'role': 'assistant', 'content': 'exporter done ' + 'x' * 8_000},
        {'role': 'user', 'content': f'Actually, scrap that — {new_goal}'},
        {'role': 'assistant', 'content': 'on it'},
    ]
    captured = {}

    def summarize(old_messages, current_query, _prefix='', **kwargs):
        return f'### Objective\n{new_goal}'

    def fake_repin(conv_id, objective, *, user_id):
        captured['repin'] = (conv_id, objective, user_id)
        return True

    monkeypatch.setattr(l2, '_generate_query_aware_summary', summarize)
    monkeypatch.setattr(l2, '_archive_transcript', lambda *args, **kwargs: None)
    monkeypatch.setattr(ap_state, '_update_objective_from_receipt', fake_repin)

    l2.execute_compact_tool(
        messages,
        task={'id': 'repin', 'convId': 'conv-repin', '_userId': 1, 'config': {}},
        preserve_budget_tokens=1,
        _compaction_skip_archive=True,
    )

    assert captured['repin'] == ('conv-repin', new_goal, 1)


@pytest.mark.unit
@pytest.mark.parametrize(
    ('thresholds', 'expected_trigger'),
    [
        ((90_000, 90_000, 0), 'window'),
        ((64_000, 90_000, 64_000), 'working_set'),
    ],
)
def test_automatic_archive_trigger_names_the_active_threshold(
    monkeypatch, thresholds, expected_trigger,
):
    """A disabled working-set ceiling must not be mislabeled as its trigger."""
    captured = {}

    def _fake_execute(messages, task=None, **kwargs):
        captured['trigger'] = kwargs['_compaction_trigger']
        kwargs['_result_meta'].update({
            'compacted': True,
            'tokens_before': 100,
            'msgs_before': len(messages),
            'archive_id': None,
        })
        return 'bounded receipt'

    monkeypatch.setattr(l2, '_should_force_compact', lambda *_a, **_k: True)
    monkeypatch.setattr(l2, '_compaction_trigger_threshold',
                        lambda *_a, **_k: thresholds)
    monkeypatch.setattr(l2, 'execute_compact_tool', _fake_execute)

    messages = [_user('continue')]
    assert l2.force_compact_if_needed(messages, task=None) is True
    assert captured['trigger'] == expected_trigger


# ═══════════════════════════════════════════════════════════════════════════
#  #1 — AUTOMATIC intra-turn fold shrinks a single giant turn
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_auto_execute_compact_folds_single_giant_turn(stub_summary):
    """★ The load-bearing fix: execute_compact_tool must fold the cold rounds
    out of a giant CURRENT turn preserved whole by the boundary — tokens drop
    hard, the summary pair is injected, and NO tool result is orphaned."""
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    from lib.tasks_pkg.compaction.api import execute_compact_tool
    from lib.tasks_pkg.compaction._constants import _INTRA_TURN_HOT_ROUNDS

    msgs = _giant_turn_api(n_rounds=40)
    before = _estimate_total_tokens(msgs)
    n_rounds_before = sum(1 for m in msgs
                          if m.get('role') == 'assistant' and m.get('tool_calls'))
    assert n_rounds_before == 40

    meta: dict = {}
    task = {'convId': 'c', 'id': 't', 'config': {'model': 'gpt-4'}}
    result = execute_compact_tool(msgs, task=task, _result_meta=meta,
                                  _compaction_skip_archive=True)

    assert meta['compacted'] is True, 'the giant turn must be compacted'
    after = _estimate_total_tokens(msgs)
    assert after < before * 0.5, (
        f'intra-turn fold must cut tokens hard: {before} → {after}')

    # Only the hot tail of tool-call rounds survives verbatim.
    n_rounds_after = sum(1 for m in msgs
                         if m.get('role') == 'assistant' and m.get('tool_calls'))
    assert n_rounds_after == _INTRA_TURN_HOT_ROUNDS, (
        f'expected {_INTRA_TURN_HOT_ROUNDS} hot rounds kept, got {n_rounds_after}')

    # No orphan tool result after the fold + summary-pair injection.
    ok, why = _api_pairs_ok(msgs)
    assert ok, f'automatic fold split a tool round: {why}'

    # The objective (leading user) is still present verbatim.
    assert any(m.get('role') == 'user' and '修复登录 bug' in (m.get('content') or '')
               for m in msgs), 'objective user turn must survive the fold'
    assert 'Compacted' in result


@pytest.mark.unit
def test_NC_auto_without_fold_leaves_giant_turn_whole(stub_summary, monkeypatch):
    """NEUTER #1: disable the intra-turn fold (make it a no-op) → the boundary
    still preserves the giant turn WHOLE, so tokens barely move and all 40
    rounds survive. Proves the fold is what does the shrinking on this shape."""
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    from lib.tasks_pkg.compaction.api import execute_compact_tool
    import lib.tasks_pkg.compaction._layer2._compact as compact_mod

    # Neuter: fold returns the region unchanged, no cold rounds extracted.
    monkeypatch.setattr(compact_mod, '_fold_recent_intra_turn',
                        lambda recent, hot_rounds=8, hot_budget_tokens=None:
                        (list(recent), []))

    msgs = _giant_turn_api(n_rounds=40)
    before = _estimate_total_tokens(msgs)
    meta: dict = {}
    task = {'convId': 'c2', 'id': 't', 'config': {'model': 'gpt-4'}}
    execute_compact_tool(msgs, task=task, _result_meta=meta,
                         _compaction_skip_archive=True)

    after = _estimate_total_tokens(msgs)
    n_rounds_after = sum(1 for m in msgs
                         if m.get('role') == 'assistant' and m.get('tool_calls'))
    # With the fold neutered the old region is only [system] → nothing folds,
    # so all 40 rounds survive and the size is essentially unchanged.
    assert n_rounds_after == 40, 'without the fold the giant turn survives whole'
    assert after > before * 0.9, (
        f'without the fold tokens must NOT drop meaningfully: {before} → {after}')


@pytest.mark.unit
def test_auto_fold_noop_on_small_turn(stub_summary):
    """A preserved turn WITHIN the hot-round tail is not folded — execute_compact
    declines gracefully (no empty summary), messages untouched."""
    from lib.tasks_pkg.compaction.api import execute_compact_tool

    msgs = _giant_turn_api(n_rounds=3)  # <= hot tail (8) → nothing to fold
    original = [dict(m) for m in msgs]
    meta: dict = {}
    task = {'convId': 'c3', 'id': 't', 'config': {'model': 'gpt-4'}}
    execute_compact_tool(msgs, task=task, _result_meta=meta,
                         _compaction_skip_archive=True)
    # Nothing foldable (old region is just [system]); declines, no mutation.
    assert meta['compacted'] is False
    assert msgs == original


@pytest.mark.unit
def test_auto_oversized_eight_round_tail_is_folded_to_budget(monkeypatch):
    """The eight-round count cap is not an unlimited token entitlement.

    A short but enormous current-turn tail must become foldable and compact,
    instead of repeatedly declining because all useful savings were protected.
    """
    import lib.tasks_pkg.compaction._layer2._compact as layer2
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    from lib.tasks_pkg.compaction.api import force_compact_if_needed
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns

    # Shape of the reported second trigger: a previous compact summary is an
    # older turn, while eight current-turn tool results own nearly all tokens.
    msgs = [
        _sys(),
        _user('original objective'),
        {'role': 'assistant', 'content': 'prior compact summary ' + 's' * 12_000},
        _user('continue the same task'),
    ]
    for i in range(8):
        msgs += _round(i, chars=48_000, dense=True)

    before = _estimate_total_tokens(msgs)
    summary_calls = []
    archive_calls = []
    monkeypatch.setattr(
        layer2, '_generate_query_aware_summary',
        lambda *a, **k: summary_calls.append((a, k)) or 'bounded summary')
    monkeypatch.setattr(
        layer2, '_archive_transcript',
        lambda *a, **k: archive_calls.append((a, k)) or 1)

    conv_id = 'low-yield-second'
    _summary_cooldowns.pop(conv_id, None)
    original = list(msgs)
    task = {
        'convId': conv_id,
        'id': 't',
        '_userId': 1,
        'config': {'model': 'kimi-k3'},
    }
    import lib.tasks_pkg.compaction._layer2._compact as compact_mod
    monkeypatch.setattr(compact_mod, '_should_force_compact', lambda *a, **k: True)
    result = force_compact_if_needed(msgs, task=task)

    assert before > 128_000  # economic trigger can genuinely be crossed
    assert result is True
    assert msgs != original
    assert len(summary_calls) == 1
    assert len(archive_calls) == 1
    assert conv_id in _summary_cooldowns
    remaining_rounds = sum(
        1 for message in msgs
        if message.get('role') == 'assistant' and message.get('tool_calls')
        and message['tool_calls'][0].get('id', '').startswith('tc_')
    )
    assert 1 <= remaining_rounds < 8
    assert _estimate_total_tokens(msgs) < before * 0.5
    assert _api_pairs_ok(msgs)[0]


@pytest.mark.unit
def test_auto_economic_decline_hysteresis_waits_for_prompt_growth(monkeypatch):
    """After a decline, the same hot tail should not be reconsidered on every
    round. The retry floor is economic only and never masks window safety."""
    import lib.tasks_pkg.compaction._tokens as tokens

    messages = [_sys(), _user('x' * 40_000)]
    msg_tokens = tokens._estimate_total_tokens(messages)
    task = {
        'convId': 'hysteresis',
        'config': {'model': 'kimi-k3'},
        '_autoCompactRetryAfterTokens': msg_tokens + 8_192,
    }
    monkeypatch.setattr(
        tokens, '_count_tokens_authoritative',
        lambda *args, **kwargs: (130_000, 'test'))

    assert tokens._should_force_compact(messages, task) is False
    assert task['_autoCompactRetryAfterTokens'] == msg_tokens + 8_192

    # Meaningful growth reaches the retry floor and clears the defer marker.
    messages.append(_user('y' * 40_000))
    monkeypatch.setattr(
        tokens, '_count_tokens_authoritative',
        lambda *args, **kwargs: (140_000, 'test'))
    assert tokens._should_force_compact(messages, task) is True
    assert '_autoCompactRetryAfterTokens' not in task


@pytest.mark.unit
def test_hysteresis_reuses_authoritative_preflight_measurement(monkeypatch):
    """Retry hysteresis must not rescan an unchanged long transcript."""
    import lib.tasks_pkg.compaction._tokens as tokens

    messages = [_sys(), _user('unchanged hot tail')]
    task = {
        'convId': 'hysteresis-measured',
        'config': {'model': 'kimi-k3'},
        '_autoCompactRetryAfterTokens': 50_000,
    }

    def count(_messages, _task, *, measurement_out=None):
        measurement_out.update({
            'message_tokens': 40_000,
            'message_count': len(_messages),
            'gate_tokens': 130_000,
            'method': 'test',
        })
        return 130_000, 'test'

    monkeypatch.setattr(tokens, '_count_tokens_authoritative', count)
    monkeypatch.setattr(
        tokens, '_estimate_total_tokens',
        lambda _messages: pytest.fail('retry gate rescanned the transcript'))

    measurement = {}
    assert tokens._should_force_compact(
        messages, task, measurement_out=measurement) is False
    assert measurement['message_tokens'] == 40_000


@pytest.mark.unit
def test_cache_negative_retry_floor_uses_optimistic_break_even_bound():
    """Do not rebuild a candidate before even all-droppable growth can pay."""
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    task = {}
    floor = layer2._defer_proactive_retry(
        task,
        130_000,
        reason='cache_negative',
        economics={
            'cache_read_tokens': 120_000,
            'dropped_tokens': 100_000,
            'cache_read_mul': 1.0,
            'rewrite_cost_tokens': 190_000,
            'summary_cost_tokens': 30_000,
        },
    )

    # One-round break-even needs 220K droppable tokens. Only 100K are
    # currently droppable, so even the optimistic lower bound needs +120K.
    assert floor == 250_000
    assert task['_autoCompactRetryWitness'] == {
        'reason': 'cache_negative',
        'cacheReadTokens': 120_000,
        'paybackLimitRounds': 1.0,
    }


@pytest.mark.unit
def test_low_yield_retry_floor_uses_optimistic_reduction_bound():
    """All-droppable growth must be enough to meet the reduction policy."""
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    task = {}
    floor = layer2._defer_proactive_retry(
        task,
        1_000_000,
        reason='low_yield',
        economics={'dropped_tokens': 0},
    )

    # At a 5% minimum reduction, x / (1M + x) >= 5%, so x must be at
    # least ceil(50K / 0.95) = 52,632 even if every new token is foldable.
    assert floor == 1_052_632
    assert '_autoCompactRetryWitness' not in task


@pytest.mark.unit
def test_cache_negative_retry_floor_yields_when_cache_witness_cools(
    monkeypatch,
):
    """A broken/cold prefix invalidates the prior warm-cache proof at once."""
    import lib.tasks_pkg.cache_tracking._state as cache_state
    import lib.tasks_pkg.compaction._tokens as tokens

    messages = [_sys(), _user('unchanged hot tail')]
    task = {
        'convId': 'cache-cooled',
        '_userId': 1,
        'config': {'model': 'kimi-k3'},
        '_autoCompactRetryAfterTokens': 250_000,
        '_autoCompactRetryWitness': {
            'reason': 'cache_negative',
            'cacheReadTokens': 120_000,
        },
    }

    def count(_messages, _task, *, measurement_out=None):
        measurement_out.update({
            'message_tokens': 130_000,
            'message_count': len(_messages),
            'gate_tokens': 130_000,
            'method': 'test',
        })
        return 130_000, 'test'

    monkeypatch.setattr(tokens, '_count_tokens_authoritative', count)
    monkeypatch.setattr(cache_state, 'get_warm_cache_read',
                        lambda *args, **kwargs: 0)

    measurement = {}
    assert tokens._should_force_compact(
        messages, task, measurement_out=measurement) is True
    assert '_autoCompactRetryAfterTokens' not in task
    assert '_autoCompactRetryWitness' not in task


@pytest.mark.unit
def test_cache_negative_retry_floor_holds_while_cache_witness_is_warm(
    monkeypatch,
):
    """Unchanged warm-cache evidence keeps the proven retry floor active."""
    import lib.tasks_pkg.cache_tracking._state as cache_state
    import lib.tasks_pkg.compaction._tokens as tokens

    messages = [_sys(), _user('unchanged hot tail')]
    task = {
        'convId': 'cache-still-warm',
        '_userId': 1,
        'config': {'model': 'kimi-k3'},
        '_autoCompactRetryAfterTokens': 250_000,
        '_autoCompactRetryWitness': {
            'reason': 'cache_negative',
            'cacheReadTokens': 120_000,
        },
    }

    def count(_messages, _task, *, measurement_out=None):
        measurement_out.update({
            'message_tokens': 130_000,
            'message_count': len(_messages),
            'gate_tokens': 130_000,
            'method': 'test',
        })
        return 130_000, 'test'

    monkeypatch.setattr(tokens, '_count_tokens_authoritative', count)
    monkeypatch.setattr(cache_state, 'get_warm_cache_read',
                        lambda *args, **kwargs: 120_000)

    measurement = {}
    assert tokens._should_force_compact(
        messages, task, measurement_out=measurement) is False
    assert task['_autoCompactRetryAfterTokens'] == 250_000
    assert task['_autoCompactRetryWitness']['cacheReadTokens'] == 120_000


@pytest.mark.unit
def test_retry_floor_never_masks_hard_window_safety(monkeypatch):
    """The real context-window trigger outranks every economic veto."""
    import lib.tasks_pkg.compaction._tokens as tokens

    messages = [_sys(), _user('oversized')]
    task = {
        'convId': 'hard-window',
        'config': {'model': 'kimi-k3'},
        '_autoCompactRetryAfterTokens': 9_000_000,
    }
    _, window_threshold, _ = tokens._compaction_trigger_threshold(task)

    def count(_messages, _task, *, measurement_out=None):
        measurement_out.update({
            'message_tokens': 130_000,
            'message_count': len(_messages),
            'gate_tokens': window_threshold + 1,
            'method': 'test',
        })
        return window_threshold + 1, 'test'

    monkeypatch.setattr(tokens, '_count_tokens_authoritative', count)
    measurement = {}
    assert tokens._should_force_compact(
        messages, task, measurement_out=measurement) is True


@pytest.mark.unit
def test_expected_decline_reuses_measurement_without_warning(
    monkeypatch, caplog,
):
    """A cache-economic no-op is normal policy, not a summary failure."""
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    captured = {}

    def should_compact(
        _messages,
        _task,
        *,
        measurement_out=None,
        current_round=None,
        remaining_api_rounds=None,
    ):
        assert current_round is None
        assert remaining_api_rounds is None
        measurement_out.update({
            'message_tokens': 12_345,
            'message_count': len(_messages),
            'gate_tokens': 130_000,
            'method': 'test',
        })
        return True

    def execute(_messages, task=None, **kwargs):
        captured['premeasured'] = kwargs['_message_tokens_before']
        kwargs['_result_meta'].update({
            'compacted': False,
            'reason': 'cache_negative',
        })
        return 'declined'

    monkeypatch.setattr(layer2, '_should_force_compact', should_compact)
    monkeypatch.setattr(layer2, 'execute_compact_tool', execute)
    caplog.set_level(logging.WARNING)

    measurement = {}
    result = layer2.force_compact_if_needed(
        [_user('continue')],
        task={'id': 't', 'convId': 'economic-noop',
              'config': {'model': 'kimi-k3'}},
        _measurement_out=measurement,
        _allow_head_truncate_fallback=True,
    )

    assert result is False
    assert captured['premeasured'] == 12_345
    assert measurement['message_tokens'] == 12_345
    assert not any(
        record.levelno >= logging.WARNING
        and 'Compaction did not mutate messages' in record.getMessage()
        for record in caplog.records)


@pytest.mark.unit
def test_unexpected_summary_failure_keeps_warning(monkeypatch, caplog):
    """Only policy declines are quiet; failed summary work stays visible."""
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    monkeypatch.setattr(
        layer2, '_should_force_compact', lambda *a, **k: True)

    def execute(_messages, task=None, **kwargs):
        kwargs['_result_meta'].update({
            'compacted': False,
            'summaryFailureReason': 'summary_failed',
        })
        return 'failed'

    monkeypatch.setattr(layer2, 'execute_compact_tool', execute)
    caplog.set_level(logging.WARNING)

    assert layer2.force_compact_if_needed(
        [_user('continue')],
        task={'id': 't', 'convId': 'summary-failed',
              'config': {'model': 'kimi-k3'}},
    ) is False
    assert any(
        record.levelno >= logging.WARNING
        and 'Compaction did not mutate messages' in record.getMessage()
        for record in caplog.records)


@pytest.mark.unit
@pytest.mark.parametrize(
    ('failure_mode', 'fallback_reason'),
    [
        ('empty', 'model_summary_unavailable'),
        ('exception', 'summary_pipeline_exception'),
    ],
)
def test_dispatch_guard_uses_deterministic_receipt_when_summary_unavailable(
    monkeypatch, failure_mode, fallback_reason,
):
    """The final guard survives both an empty result and a pipeline defect."""
    import lib.tasks_pkg.commit_round._turn_diff as turn_diff
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    def unavailable_summary(*_args, **_kwargs):
        if failure_mode == 'exception':
            raise TypeError('fault-injected summary pipeline defect')
        return None

    monkeypatch.setattr(
        layer2, '_generate_query_aware_summary', unavailable_summary)
    monkeypatch.setattr(
        layer2, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(
        layer2, '_extract_recently_accessed_files',
        lambda _messages: ['/oversized/' + ('f' * 30_000)],
    )
    monkeypatch.setattr(
        turn_diff, 'build_turn_diff_block',
        lambda *a, **k: 'oversized diff ' + ('d' * 30_000),
    )
    receipt_calls = []
    real_build_receipt = layer2.build_compaction_receipt

    def capture_receipt(**kwargs):
        receipt_calls.append(kwargs)
        return real_build_receipt(**kwargs)

    monkeypatch.setattr(layer2, 'build_compaction_receipt', capture_receipt)

    objective = r'Finish the parser for C:\users\name and preserve \u003cplan.'
    messages = [
        _sys(),
        _user(objective),
        {'role': 'assistant', 'content': 'old state ' + ('x' * 20_000)},
        _user('Continue with the remaining tests.'),
        {'role': 'assistant', 'content': 'current state'},
    ]
    task = {
        'id': 't',
        'convId': 'deterministic-recovery',
        'config': {'model': 'kimi-k3'},
    }
    meta = {}

    compacted = layer2.force_compact_if_needed(
        messages,
        task=task,
        preserve_budget_tokens=1,
        force=True,
        _allow_deterministic_summary_fallback=True,
        _compaction_skip_archive=True,
        _result_meta=meta,
    )

    assert compacted is True
    assert meta['compacted'] is True
    assert meta['summaryFallback'] is True
    assert meta['summaryFallbackReason'] == fallback_reason
    assert len(meta['summary_text']) <= layer2._DETERMINISTIC_RECOVERY_MAX_CHARS
    assert meta['turn_diff_included'] is False
    assert any(message.get('content') == objective for message in messages)
    assert any(message.get('content') == 'Continue with the remaining tests.'
               for message in messages)
    receipt_text = messages[-1]['content']
    assert '## Deterministic Compaction Recovery' in receipt_text
    assert '## TaskStateSnapshotV1' in receipt_text
    assert receipt_calls[-1]['implementation'] == (
        'deterministic_recovery_receipt')
    assert receipt_calls[-1]['summary_generated'] is False
    assert receipt_calls[-1]['outcome_reason'] == fallback_reason


@pytest.mark.unit
def test_deterministic_task_state_projection_is_valid_json_and_bounded():
    """Recovery state has one global request-size budget, not per-field caps."""
    class OversizedSnapshot:
        def to_dict(self):
            return {
                'contract_version': 'tofu.task-state/v1',
                'goal': 'g' * 20_000,
                'hard_constraints': tuple('c' * 2_000 for _ in range(40)),
                'decisions': tuple('d' * 2_000 for _ in range(40)),
                'completed_work': tuple('w' * 2_000 for _ in range(40)),
                'files': tuple('/path/' + ('f' * 2_000) for _ in range(40)),
                'tests': tuple('t' * 2_000 for _ in range(40)),
                'errors': tuple('e' * 2_000 for _ in range(40)),
                'todos': tuple('o' * 2_000 for _ in range(40)),
                'world_version': 'v' * 20_000,
                'source_digest': 'digest',
            }

    rendered = l2._bounded_task_state_text(
        OversizedSnapshot(), max_chars=12_000)

    assert len(rendered) <= 12_000
    payload = json.loads(rendered)
    assert payload['contract_version'] == 'tofu.task-state/v1'
    assert isinstance(payload['decisions'], list)


@pytest.mark.unit
def test_deterministic_recovery_receipt_has_one_global_character_ceiling():
    messages = [_sys(), _user('goal')]
    task = {
        '_contextEvidenceLedger': {
            'version': 1,
            'entries': [
                {
                    'id': f'ev-{index}',
                    'type': 'error',
                    'source': 'fault-injection',
                    'value': 'e' * 10_000,
                }
                for index in range(96)
            ],
            'evidenceIds': [f'ev-{index}' for index in range(96)],
        },
        '_todos': ['t' * 10_000 for _ in range(96)],
        '_nextSteps': ['n' * 10_000 for _ in range(96)],
    }

    rendered = l2._deterministic_recovery_summary(messages, task)

    assert len(rendered) <= l2._DETERMINISTIC_RECOVERY_MAX_CHARS
    assert '## Deterministic Compaction Recovery' in rendered
    assert '## TaskStateSnapshotV1' in rendered


@pytest.mark.unit
def test_auto_summary_cost_declines_before_model_dispatch(monkeypatch):
    """Expected summary cost belongs in preflight, before it becomes sunk."""
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    summary_calls = []
    monkeypatch.setattr(
        layer2, '_generate_query_aware_summary',
        lambda *args, **kwargs: summary_calls.append((args, kwargs)) or 'receipt')
    monkeypatch.setattr(
        layer2, '_projected_summary_usage_tokens',
        lambda *args, **kwargs: 12_000)

    def economics(_task, *, tokens_before, candidate_tokens,
                  summary_usage_tokens=0):
        dropped = max(1, tokens_before - candidate_tokens)
        return {
            'cache_read_tokens': 100_000,
            'cache_rewrite_tokens': candidate_tokens,
            'dropped_tokens': dropped,
            'cache_write_mul': 1.0,
            'cache_read_mul': 1.0,
            'rewrite_cost_tokens': candidate_tokens,
            'summary_cost_tokens': summary_usage_tokens,
            'payback_rounds': 2.0 if summary_usage_tokens else 0.5,
            'pricing_source': 'test',
        }

    monkeypatch.setattr(layer2, '_proactive_cache_economics', economics)
    messages = [
        _sys(), _user('objective'),
        {'role': 'assistant', 'content': 'old state ' + ('x' * 20_000)},
        _user('continue'), {'role': 'assistant', 'content': 'current state'},
    ]
    meta = {}
    layer2.execute_compact_tool(
        messages, task={'convId': 'preflight', 'id': 't',
                        'config': {'model': 'gpt-4'}},
        preserve_budget_tokens=1, _proactive_economic=True,
        _compaction_skip_archive=True, _result_meta=meta)

    assert summary_calls == []
    assert meta['compacted'] is False
    assert meta['reason'] == 'cache_negative'
    assert meta['projected_summary_cost_tokens'] == 12_000


@pytest.mark.unit
def test_adaptive_expected_horizon_survives_fixed_one_round_gate(monkeypatch):
    """The adaptive PEV decision must reach both exact L2 ROI checks.

    A three-round candidate is intentionally uneconomic for the shipped fixed
    policy, but profitable inside this adaptive request's six-round horizon.
    The old wiring admitted it in ``_should_force_compact`` and then silently
    rejected it here against the fixed one-round constant.
    """
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    summary_calls = []
    monkeypatch.setattr(
        layer2, '_projected_summary_usage_tokens',
        lambda *args, **kwargs: 10_000)

    def summary(*args, **kwargs):
        summary_calls.append((args, kwargs))
        kwargs['usage_out'].update({
            'prompt_tokens': 8_000,
            'completion_tokens': 500,
        })
        return 'Objective preserved; continue the current implementation.'

    monkeypatch.setattr(layer2, '_generate_query_aware_summary', summary)

    def economics(_task, *, tokens_before, candidate_tokens,
                  summary_usage_tokens=0):
        dropped = max(1, tokens_before - candidate_tokens)
        return {
            'cache_read_tokens': 100_000,
            'cache_rewrite_tokens': candidate_tokens,
            'dropped_tokens': dropped,
            'cache_write_mul': 1.0,
            'cache_read_mul': 0.1,
            'rewrite_cost_tokens': candidate_tokens,
            'summary_cost_tokens': summary_usage_tokens,
            'payback_rounds': 3.0,
            'pricing_source': 'test',
        }

    monkeypatch.setattr(layer2, '_proactive_cache_economics', economics)
    messages = [
        _sys(), _user('Objective: finish the implementation.'),
        {'role': 'assistant', 'content': 'old state ' + ('x' * 20_000)},
        _user('continue'), {'role': 'assistant', 'content': 'current state'},
    ]
    task = {
        'convId': 'adaptive-horizon',
        'id': 't',
        'config': {
            'model': 'kimi-k3',
            'compaction': {'strategy': 'adaptive'},
        },
        '_adaptiveCompactionDecision': {
            'shouldTrigger': True,
            'remainingRoundsMedian': 6.0,
        },
    }
    meta = {}

    layer2.execute_compact_tool(
        messages, task=task, preserve_budget_tokens=1,
        _proactive_economic=True, _compaction_skip_archive=True,
        _result_meta=meta)

    assert len(summary_calls) == 1
    assert meta['compacted'] is True
    assert meta['economics']['payback_rounds'] == 3.0
    assert meta['economics']['payback_limit_rounds'] == 6.0
    assert meta['economics']['payback_policy'] == 'adaptive_expected_horizon'


@pytest.mark.unit
def test_auto_does_not_reject_paid_summary_on_sunk_summary_cost(monkeypatch):
    """After generation, adoption compares only future rewrite economics."""
    import lib.tasks_pkg.compaction._layer2._compact as layer2

    monkeypatch.setattr(
        layer2, '_projected_summary_usage_tokens',
        lambda *args, **kwargs: 0)

    def summary(*args, **kwargs):
        kwargs['usage_out'].update({'prompt_tokens': 8_000,
                                    'completion_tokens': 4_000})
        return 'small faithful receipt'

    monkeypatch.setattr(layer2, '_generate_query_aware_summary', summary)

    def economics(_task, *, tokens_before, candidate_tokens,
                  summary_usage_tokens=0):
        dropped = max(1, tokens_before - candidate_tokens)
        return {
            'cache_read_tokens': 100_000,
            'cache_rewrite_tokens': candidate_tokens,
            'dropped_tokens': dropped,
            'cache_write_mul': 1.0,
            'cache_read_mul': 1.0,
            'rewrite_cost_tokens': candidate_tokens,
            'summary_cost_tokens': summary_usage_tokens,
            'payback_rounds': 2.0 if summary_usage_tokens else 0.5,
            'pricing_source': 'test',
        }

    monkeypatch.setattr(layer2, '_proactive_cache_economics', economics)
    messages = [
        _sys(), _user('objective'),
        {'role': 'assistant', 'content': 'old state ' + ('x' * 20_000)},
        _user('continue'), {'role': 'assistant', 'content': 'current state'},
    ]
    meta = {}
    layer2.execute_compact_tool(
        messages, task={'convId': 'sunk-cost', 'id': 't',
                        'config': {'model': 'gpt-4'}},
        preserve_budget_tokens=1, _proactive_economic=True,
        _compaction_skip_archive=True, _result_meta=meta)

    assert meta['compacted'] is True
    assert meta['summary_usage_tokens'] == 12_000
    assert meta['tokens_after_estimated'] > 0


@pytest.mark.unit
def test_auto_realized_low_yield_candidate_is_not_committed(monkeypatch):
    """Best-case eligibility is only a prefilter.  If the generated summary
    plus its synthetic pair leaves <5% realized savings, proactive L2 must
    preserve the live prefix and report no completed compaction side effects."""
    import lib.context_telemetry as telemetry
    import lib.tasks_pkg.cache_tracking._roi as cache_tracking
    import lib.tasks_pkg.compaction._layer2._compact as layer2
    import lib.tasks_pkg.compaction._layer2._compact as compact_mod
    import lib.token_counter as token_counter
    from lib.tasks_pkg.compaction.api import force_compact_if_needed
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns

    msgs = [
        _sys(),
        _user('objective'),
        {'role': 'assistant', 'content': 'old answer ' + 'a' * 160_000},
        _user('continue'),
        {'role': 'assistant', 'content': 'protected recent answer ' + 'b' * 60_000},
    ]
    original = [dict(m) for m in msgs]
    conv_id = 'realized-low-yield'
    _summary_cooldowns.pop(conv_id, None)
    task = {
        'convId': conv_id,
        'id': 't',
        'config': {'model': 'kimi-k3'},
        '_contextEvidenceLedger': {'entries': [{'id': 'temporary'}]},
    }

    summary_calls = []
    archive_calls = []
    roi_calls = []
    telemetry_calls = []
    invalidations = []
    monkeypatch.setattr(compact_mod, '_should_force_compact', lambda *a, **k: True)
    monkeypatch.setattr(
        layer2, '_generate_query_aware_summary',
        lambda *a, **k: summary_calls.append((a, k)) or ('summary ' + 'z' * 155_000))
    monkeypatch.setattr(
        layer2, '_archive_transcript',
        lambda *a, **k: archive_calls.append((a, k)) or 17)
    monkeypatch.setattr(
        cache_tracking, 'record_l2_compaction',
        lambda *a, **k: roi_calls.append((a, k)))
    monkeypatch.setattr(
        telemetry, 'record_compaction_event',
        lambda *a, **k: telemetry_calls.append((a, k)))
    monkeypatch.setattr(
        token_counter, 'invalidate', lambda conv: invalidations.append(conv))

    result = force_compact_if_needed(msgs, task=task)

    assert result is False
    assert msgs == original
    assert len(summary_calls) == 1, 'best-case gate should permit candidate generation'
    assert archive_calls == []
    assert roi_calls == []
    assert telemetry_calls == []
    assert invalidations == []
    assert conv_id not in _summary_cooldowns
    assert '_contextEvidenceLedger' not in task
    assert not any(e.get('type') in ('compaction', 'compaction_done')
                   for e in task.get('events', []))


@pytest.mark.unit
def test_forced_compaction_bypasses_realized_yield_gate(monkeypatch):
    """Manual/reactive force=True is correctness-first: even a summary that
    realizes <5% savings still commits and injects the synthetic pair."""
    import lib.tasks_pkg.compaction._layer2._compact as layer2
    from lib.tasks_pkg.compaction.api import force_compact_if_needed

    msgs = [
        _sys(),
        _user('objective'),
        {'role': 'assistant', 'content': 'old answer ' + 'a' * 40_000},
        _user('continue'),
        {'role': 'assistant', 'content': 'protected recent answer ' + 'b' * 60_000},
    ]
    original = [dict(m) for m in msgs]
    monkeypatch.setattr(
        layer2, '_generate_query_aware_summary',
        lambda *a, **k: 'summary ' + 'z' * 38_000)
    monkeypatch.setattr(layer2, '_archive_transcript', lambda *a, **k: None)

    task = {
        'convId': 'forced-low-yield',
        'id': 't',
        '_userId': 1,
        'config': {'model': 'kimi-k3'},
        '_contextEvidenceLedger': {'entries': [{'id': 'temporary'}]},
    }
    result = force_compact_if_needed(msgs, task=task, force=True,
                                     keep_recent_pairs=1)

    assert result is True
    assert msgs != original
    assert '_contextEvidenceLedger' not in task
    assert any(m.get('role') == 'tool' and m.get('name') == 'context_compact'
               for m in msgs)


# ═══════════════════════════════════════════════════════════════════════════
#  #1b — SUCCESS-PATH CONVERGENCE: fold+summary succeeds but the preserved
#        hot-tail rounds are themselves oversized → execute_compact_tool must
#        converge the PROJECTED request under the trigger ceiling in the SAME
#        round, not defer to next-round / reactive-413.
# ═══════════════════════════════════════════════════════════════════════════

def _ceiling_for(task):
    """The same ceiling execute_compact_tool checks against: usable × ratio."""
    from lib.tasks_pkg.compaction._tokens import _get_context_limit, _usable_context
    from lib.tasks_pkg.compaction._constants import _SUMMARY_TRIGGER_RATIO
    usable = _usable_context(_get_context_limit(task))
    return int(usable * _SUMMARY_TRIGGER_RATIO)


@pytest.mark.unit
def test_auto_compact_token_budget_converges_without_oversized_hot_tail(stub_summary):
    """★ The token-aware hot suffix fits before the emergency convergence net.

    Whole-round folding must keep the result under the ceiling, paired, and
    objective-preserving even when the newest eight rounds would not fit.
    """
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    from lib.tasks_pkg.compaction.api import execute_compact_tool

    # gpt-4 → 128k window; each hot round ~45k DENSE chars so 8 hot rounds
    # alone blow past the ~80.6k-token ceiling even after the cold body is
    # folded. Dense matters: the convergence projection counts tokens
    # AUTHORITATIVELY (tiktoken), and repetitive filler would BPE-merge to
    # under the ceiling, skipping the very path under test.
    task = {'convId': 'conv_conv', 'id': 't', 'config': {'model': 'gpt-4'}}
    ceiling = _ceiling_for(task)
    msgs = _giant_turn_api(n_rounds=40, chars=45_000, dense=True)

    meta: dict = {}
    execute_compact_tool(msgs, task=task, _result_meta=meta,
                         _compaction_skip_archive=True)

    assert meta['compacted'] is True, 'fold+summary must have succeeded'
    after = _estimate_total_tokens(msgs)
    assert after <= ceiling, (
        f'convergence must bring the preserved region under the ceiling: '
        f'{after} > {ceiling}')
    ok, why = _api_pairs_ok(msgs)
    assert ok, f'convergence head-truncate orphaned a tool result: {why}'
    # The objective (leading user) survives the convergence truncation.
    assert any(m.get('role') == 'user' and '修复登录 bug' in (m.get('content') or '')
               for m in msgs), 'objective must survive success-path convergence'


@pytest.mark.unit
def test_NC_count_only_hot_tail_leaves_projected_over_ceiling(stub_summary, monkeypatch):
    """NEUTER #1b: restore count-only retention and disable head truncation.

    Eight huge rounds then survive over the ceiling, proving token-budgeted
    whole-round selection—not the round-count cap—is the load-bearing fix.
    """
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens
    from lib.tasks_pkg.compaction.api import execute_compact_tool
    import lib.tasks_pkg.compaction._layer2._compact as compact_mod
    from lib.tasks_pkg.compaction._layer2._anchor import (
        _fold_recent_intra_turn as real_fold,
    )
    import lib.tasks_pkg.compaction._reactive as reactive_mod

    monkeypatch.setattr(
        compact_mod, '_fold_recent_intra_turn',
        lambda recent, hot_rounds=8, hot_budget_tokens=None:
        real_fold(recent, hot_rounds=hot_rounds, hot_budget_tokens=None))
    # Neuter: the convergence check calls this and drops nothing.
    monkeypatch.setattr(reactive_mod, '_head_truncate',
                        lambda *a, **k: 0)

    task = {'convId': 'conv_nc', 'id': 't', 'config': {'model': 'gpt-4'}}
    ceiling = _ceiling_for(task)
    msgs = _giant_turn_api(n_rounds=40, chars=45_000, dense=True)

    meta: dict = {}
    execute_compact_tool(msgs, task=task, _result_meta=meta,
                         _compaction_skip_archive=True)

    assert meta['compacted'] is True
    after = _estimate_total_tokens(msgs)
    assert after > ceiling, (
        f'without convergence the oversized hot tail must stay over the '
        f'ceiling: {after} <= {ceiling} (neuter failed to expose the gap)')


# ═══════════════════════════════════════════════════════════════════════════
#  #2 — head-truncate NEVER splits a tool pair
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_head_truncate_never_orphans_a_tool_pair():
    """★ The emergency net must drop whole tool-call rounds — after an
    aggressive token-target truncation, every surviving ``tool`` result still
    has its ``assistant.tool_calls`` parent (no HTTP-400 orphan)."""
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    # system + objective(user) + 40 heavy tool-call rounds (single turn).
    msgs = _giant_turn_api(n_rounds=40, chars=4000)
    task = {'convId': 'c', 'id': 't', 'config': {'model': 'gpt-4'}}
    dropped = _head_truncate(msgs, task, reported_token_count=10_000_000)
    assert dropped > 0, 'aggressive target must drop something'

    ok, why = _api_pairs_ok(msgs)
    assert ok, f'head-truncate orphaned a tool result: {why}'
    # The very first live message after system must NOT be a bare tool result.
    first_non_sys = next((m for m in msgs if m.get('role') != 'system'), None)
    assert first_non_sys is None or first_non_sys.get('role') != 'tool', (
        'head-truncate left an orphan tool result at the head')


@pytest.mark.unit
def test_head_truncate_byte_target_never_orphans():
    """Same guarantee on the BYTE-target branch (the 413 wire-size path)."""
    from lib.tasks_pkg.compaction._reactive._headtrunc import _head_truncate

    msgs = _giant_turn_api(n_rounds=30, chars=8000)
    task = {'convId': 'c', 'id': 't', 'config': {'model': 'gpt-4'}}
    # Tiny byte target forces heavy dropping.
    dropped = _head_truncate(msgs, task, byte_target=50_000)
    assert dropped > 0
    ok, why = _api_pairs_ok(msgs)
    assert ok, f'byte-target head-truncate orphaned a tool result: {why}'


@pytest.mark.unit
def test_NC_naive_per_message_head_truncate_orphans_tool():
    """NEUTER #2: a naive per-message pop that stops as soon as the size target
    is met splits the round it stops inside — leaving a ``tool`` result whose
    ``assistant(tool_calls)`` was popped. Proves the round-aware unit is
    load-bearing (revert → this orphan reappears)."""
    msgs = _giant_turn_api(n_rounds=40, chars=4000)
    system_end = 1  # one system message

    # Reference NAIVE loop: pop single oldest non-system message (protect the
    # objective anchor at index 1). Popping an ODD number of messages
    # deterministically stops AFTER an assistant(tool_calls) but BEFORE its
    # ``tool`` result — the exact mid-round split the round-aware unit prevents.
    def _pos():
        # protect the objective anchor (user) at system_end
        if msgs[system_end].get('role') == 'user' and len(msgs) > system_end + 1:
            return system_end + 1
        return system_end

    for _ in range(15):  # odd count → ends mid-round, stranding a tool result
        msgs.pop(_pos())

    ok, _why = _api_pairs_ok(msgs)
    assert not ok, ('the naive per-message truncation SHOULD orphan a tool '
                    'result — that is exactly the bug the round-aware unit fixes')


# ═══════════════════════════════════════════════════════════════════════════
#  #3 — the manual + automatic paths share ONE fold boundary
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_shared_split_policy_matches_both_paths():
    """``_split_cold_rounds`` is the single cut both paths use. The api-form
    fold and the manual raw fold must agree on how many rounds are HOT vs COLD
    for the same round count + hot-tail."""
    from lib.tasks_pkg.compaction._layer2 import (
        _apiform_tool_rounds, _fold_recent_intra_turn, _split_cold_rounds)
    from lib.tasks_pkg.compaction._constants import _INTRA_TURN_HOT_ROUNDS

    # api-form region: user + 40 rounds.
    msgs = [_user('go')]
    for i in range(40):
        msgs += _round(i, chars=100)
    kept, cold = _fold_recent_intra_turn(msgs)
    hot_rounds_kept = sum(1 for m in kept
                          if m.get('role') == 'assistant' and m.get('tool_calls'))
    cold_rounds = len({m['tool_call_id'] for m in cold if m.get('role') == 'tool'})
    assert hot_rounds_kept == _INTRA_TURN_HOT_ROUNDS
    assert cold_rounds == 40 - _INTRA_TURN_HOT_ROUNDS

    # Same policy on a bare round list (manual path uses this element-agnostic).
    fake_rounds = list(range(40))
    c, h = _split_cold_rounds(fake_rounds)
    assert len(h) == _INTRA_TURN_HOT_ROUNDS
    assert len(c) == 40 - _INTRA_TURN_HOT_ROUNDS

    # And _apiform_tool_rounds finds exactly 40 spans (the user row is not one).
    assert len(_apiform_tool_rounds(msgs)) == 40


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
