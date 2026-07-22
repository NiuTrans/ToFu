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
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.tasks_pkg.compaction._layer2 as l2


# ── api-form builders ──────────────────────────────────────────────────────

def _sys():
    return {'role': 'system', 'content': 'you are a coding assistant'}


def _user(text):
    return {'role': 'user', 'content': text}


def _round(i, chars=4000):
    """One api-form tool-call ROUND: assistant(tool_calls) + its tool result."""
    tcid = f'tc_{i}'
    return [
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': tcid, 'type': 'function',
                         'function': {'name': 'read_files',
                                      'arguments': '{"path": "x"}'}}]},
        {'role': 'tool', 'tool_call_id': tcid, 'name': 'read_files',
         'content': 'RESULT ' + ('x' * chars)},
    ]


def _giant_turn_api(n_rounds=40, chars=4000):
    """system + user(objective) + ONE turn of n_rounds tool-call rounds (no
    intervening user), i.e. a single agentic turn that fills the window."""
    msgs = [_sys(), _user('修复登录 bug，尽可能彻底')]
    for i in range(n_rounds):
        msgs += _round(i, chars=chars)
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

    # gpt-4 → 128k window; each hot round ~45k chars so 8 hot rounds alone
    # blow past the ~80.6k-token ceiling even after the cold body is folded.
    task = {'convId': 'conv_conv', 'id': 't', 'config': {'model': 'gpt-4'}}
    ceiling = _ceiling_for(task)
    msgs = _giant_turn_api(n_rounds=40, chars=45_000)

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
    msgs = _giant_turn_api(n_rounds=40, chars=45_000)

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
