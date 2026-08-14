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

import os
import random
import string
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.tasks_pkg.compaction._layer2 as l2


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
    def _fake(old_messages, current_query, log_prefix='', conv_id='', task=None):
        return '### 1. Primary Request\n[folded earlier tool rounds summarized]'
    monkeypatch.setattr(l2, '_generate_query_aware_summary', _fake)
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)
    return _fake


# ═══════════════════════════════════════════════════════════════════════════
#  #1 — AUTOMATIC intra-turn fold shrinks a single giant turn
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_auto_execute_compact_folds_single_giant_turn(stub_summary):
    """★ The load-bearing fix: execute_compact_tool must fold the cold rounds
    out of a giant CURRENT turn preserved whole by the boundary — tokens drop
    hard, the summary pair is injected, and NO tool result is orphaned."""
    from lib.tasks_pkg.compaction import (
        _estimate_total_tokens, execute_compact_tool)
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
    from lib.tasks_pkg.compaction import (
        _estimate_total_tokens, execute_compact_tool)
    import lib.tasks_pkg.compaction._layer2._compact as compact_mod

    # Neuter: fold returns the region unchanged, no cold rounds extracted.
    monkeypatch.setattr(compact_mod, '_fold_recent_intra_turn',
                        lambda recent, hot_rounds=8: (list(recent), []))

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
    from lib.tasks_pkg.compaction import execute_compact_tool

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
def test_auto_low_yield_second_compaction_declines_before_summary(monkeypatch):
    """A threshold crossing is not sufficient when almost all tokens are in
    the protected hot region.  Automatic L2 must decline before paying for a
    summary or rewriting the cache prefix when the best-case fold is <5%."""
    import lib.tasks_pkg.compaction._layer2 as layer2
    from lib.tasks_pkg.compaction import (
        _estimate_total_tokens, force_compact_if_needed)
    from lib.tasks_pkg.compaction._constants import _summary_cooldowns

    # Shape of the reported second trigger: a previous compact summary is an
    # older turn, while the current turn's hot tool results own nearly all of
    # the prompt.  Folding the older turn can save only ~3%.
    msgs = [
        _sys(),
        _user('original objective'),
        {'role': 'assistant', 'content': 'prior compact summary ' + 's' * 12_000},
        _user('continue the same task'),
    ]
    for i in range(8):  # protected hot tail: not eligible for intra-turn fold
        msgs += _round(i, chars=48_000, dense=True)

    before = _estimate_total_tokens(msgs)
    summary_calls = []
    archive_calls = []
    monkeypatch.setattr(
        layer2, '_generate_query_aware_summary',
        lambda *a, **k: summary_calls.append((a, k)) or 'SHOULD NOT RUN')
    monkeypatch.setattr(
        layer2, '_archive_transcript',
        lambda *a, **k: archive_calls.append((a, k)) or 1)

    conv_id = 'low-yield-second'
    _summary_cooldowns.pop(conv_id, None)
    original = list(msgs)
    task = {
        'convId': conv_id,
        'id': 't',
        'config': {'model': 'kimi-k3'},
    }
    import lib.tasks_pkg.compaction._layer2._compact as compact_mod
    monkeypatch.setattr(compact_mod, '_should_force_compact', lambda *a, **k: True)
    result = force_compact_if_needed(msgs, task=task)

    assert before > 128_000  # economic trigger can genuinely be crossed
    assert result is False
    assert msgs == original
    assert summary_calls == [], 'declined rewrite must not spend summary tokens'
    assert archive_calls == [], 'declined rewrite must not emit a fake snapshot'
    assert conv_id not in _summary_cooldowns
    assert task['_autoCompactRetryAfterTokens'] > before


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
def test_auto_realized_low_yield_candidate_is_not_committed(monkeypatch):
    """Best-case eligibility is only a prefilter.  If the generated summary
    plus its synthetic pair leaves <5% realized savings, proactive L2 must
    preserve the live prefix and report no completed compaction side effects."""
    import lib.context_telemetry as telemetry
    import lib.tasks_pkg.cache_tracking as cache_tracking
    import lib.tasks_pkg.compaction._layer2 as layer2
    import lib.tasks_pkg.compaction._layer2._compact as compact_mod
    import lib.token_counter as token_counter
    from lib.tasks_pkg.compaction import force_compact_if_needed
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
    import lib.tasks_pkg.compaction._layer2 as layer2
    from lib.tasks_pkg.compaction import force_compact_if_needed

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
def test_auto_compact_converges_when_hot_tail_still_overflows(stub_summary):
    """★ Fold + summary succeed, but the 8 preserved HOT rounds are each so
    large that the projected request still exceeds the trigger ceiling. The
    success-path convergence check must head-truncate (pairing-safe) so the
    result fits the window THIS round — no orphan, objective preserved."""
    from lib.tasks_pkg.compaction import (
        _estimate_total_tokens, execute_compact_tool)

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
def test_NC_no_convergence_leaves_projected_over_ceiling(stub_summary, monkeypatch):
    """NEUTER #1b: neuter the convergence head-truncate (make it a no-op) → the
    oversized hot tail survives whole and the preserved region stays OVER the
    ceiling. Proves the success-path convergence check is what bounds it (revert
    → the over-window request reappears)."""
    from lib.tasks_pkg.compaction import (
        _estimate_total_tokens, execute_compact_tool)
    import lib.tasks_pkg.compaction._reactive as reactive_mod

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
    from lib.tasks_pkg.compaction import _head_truncate

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
    from lib.tasks_pkg.compaction import _head_truncate

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
