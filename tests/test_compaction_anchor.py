"""Layer-2 compaction anchor / boundary / intra-turn-fold semantics.

WHY THIS FILE EXISTS
────────────────────
`lib/tasks_pkg/compaction/_layer2/_anchor.py` decides WHICH user context
survives compaction.  Every defect in it is SILENT — nothing raises, no
error envelope is emitted, the conversation just quietly loses the user's
original goal or gets its in-flight turn truncated.  That makes it the
hardest class of regression to notice in production and the cheapest to
pin with tests, since every function here is PURE (no LLM, no DB, no I/O).

Measured before this file existed: 47% line coverage, zero tests naming
the module.

WHAT IS ASSERTED (results, not implementation — charter discipline)
  * OBJECTIVE ANCHOR resolves to the human's FIRST REAL user turn, skipping
    leading system rows, the synthetic `_isMeta` context carriers the builder
    prepends (CLAUDE.md / preference profile), and autopilot VU turns.
    Anchoring on an injected carrier would protect CLAUDE.md verbatim across
    N summaries while the actual goal decays away.
  * The turn boundary NEVER splits a turn and ALWAYS preserves the current
    one, even when that single turn alone blows the whole budget.
  * The intra-turn fold cuts on WHOLE tool rounds, so a `tool` message can
    never be orphaned from its `assistant(tool_calls)` parent — an orphan is
    a hard 400 from the upstream API, i.e. a broken conversation.
  * `_split_cold_rounds` is the SINGLE shared cut used by both the manual
    `/compact` path and the automatic L2 path; both index spaces must land on
    the same keep-vs-fold line or the two paths silently diverge.
  * `_coerce_spec_list` never iterates a JSON string char-by-char (the real
    "one letter per line" modified-files incident, conv mr4e8pnxbv440z).
"""

import pytest

from lib.tasks_pkg.compaction._layer2._anchor import (
    _apiform_tool_rounds,
    _coerce_spec_list,
    _extract_current_query,
    _extract_recently_accessed_files,
    _find_turn_boundary,
    _fold_recent_intra_turn,
    _objective_anchor_index,
    _split_cold_rounds,
)

pytestmark = pytest.mark.unit


def _u(text, **extra):
    return {'role': 'user', 'content': text, **extra}


def _a(text, **extra):
    return {'role': 'assistant', 'content': text, **extra}


# ───────────────────────── OBJECTIVE ANCHOR ─────────────────────────

def test_anchor_is_first_real_user_turn():
    msgs = [{'role': 'system', 'content': 'sys'}, _u('build me a parser'), _a('ok')]
    assert _objective_anchor_index(msgs) == 1


def test_anchor_skips_injected_meta_carrier():
    """The builder prepends CLAUDE.md / preferences as a `user` row at index 1.

    Anchoring there would protect injected context verbatim forever while the
    human's actual goal gets summarized away — the exact inversion of intent.
    """
    msgs = [
        {'role': 'system', 'content': 'sys'},
        _u('# CLAUDE.md project rules', _isMeta=True),
        _u('the real goal'),
    ]
    assert _objective_anchor_index(msgs) == 2


def test_anchor_skips_autopilot_vu_turns():
    msgs = [
        _u('VU directive', _isVuDirective=True),
        _u('virtual reply', _isVirtualUser=True),
        _u('human goal'),
    ]
    assert _objective_anchor_index(msgs) == 2


def test_anchor_skips_blank_user_rows():
    """A whitespace-only user row is not a goal."""
    assert _objective_anchor_index([_u('   '), _u('\n'), _u('actual')]) == 2


def test_anchor_reads_text_blocks_in_list_content():
    msgs = [_u([{'type': 'text', 'text': '  '}]), _u([{'type': 'text', 'text': 'goal'}])]
    assert _objective_anchor_index(msgs) == 1


def test_anchor_accepts_an_image_only_opening_turn():
    """A conversation opened with just a screenshot IS anchored on that turn.

    Regression guard for pt_683c4550: the list branch used to count only
    `type == 'text'` blocks, so an image-only turn looked empty and was
    skipped — the anchor landed on a LATER turn and the user's actual request
    ("fix this" + screenshot) became eligible for summarizing away, cumulative
    over every compaction. The `elif content:` branch that documented this case
    was unreachable for list content.

    Deliberately DIFFERENT from autopilot_state._extract_objective, which
    returns TEXT for the virtual user and is right to skip an image-only turn.
    Here the return value is an INDEX meaning "protect this message", so an
    image carries the goal just as well as text.
    """
    msgs = [_u([{'type': 'image', 'source': {}}]), _u('later text')]
    assert _objective_anchor_index(msgs) == 0


def test_anchor_accepts_mixed_image_plus_blank_text():
    """Clients often emit an empty text block alongside the image."""
    msgs = [_u([{'type': 'text', 'text': '  '}, {'type': 'image', 'source': {}}]),
            _u('later')]
    assert _objective_anchor_index(msgs) == 0


def test_anchor_still_skips_a_genuinely_empty_list_turn():
    """The fix must not make EVERY list-content turn count — an empty list (or
    one holding only blank text) is still not a goal."""
    msgs = [_u([]), _u([{'type': 'text', 'text': '\n'}]), _u('real')]
    assert _objective_anchor_index(msgs) == 2


def test_anchor_none_when_no_real_user_message():
    """No anchor → compaction behaves exactly as it did pre-anchor."""
    assert _objective_anchor_index([{'role': 'system', 'content': 's'}, _a('hi')]) is None
    assert _objective_anchor_index([]) is None


def test_anchor_tolerates_non_dict_rows():
    assert _objective_anchor_index(['garbage', None, _u('goal')]) == 2


# ───────────────────────── current query ─────────────────────────

def test_current_query_takes_the_newest_user_turn():
    msgs = [_u('old'), _a('reply'), _u('newest')]
    assert _extract_current_query(msgs) == 'newest'


def test_current_query_joins_text_blocks_and_truncates():
    msgs = [_u([{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}])]
    assert _extract_current_query(msgs) == 'a\nb'
    assert len(_extract_current_query([_u('x' * 5000)])) == 500


def test_current_query_empty_when_no_user_turn():
    assert _extract_current_query([_a('only assistant')]) == ''


# ───────────────────────── turn boundary ─────────────────────────

def test_boundary_lands_on_a_user_index_and_never_splits_a_turn():
    msgs = [_u('t1'), _a('r1'), _u('t2'), _a('r2'), _u('t3'), _a('r3')]
    b = _find_turn_boundary(msgs, budget_tokens=float('inf'))
    assert msgs[b]['role'] == 'user'


def test_boundary_preserves_current_turn_even_when_over_budget():
    """HARD INVARIANT: budget=0 must still keep the whole current turn.

    Dropping the in-flight turn to satisfy a budget would delete the request
    the model is answering right now.
    """
    msgs = [_u('old'), _a('x'), _u('current'), _a('y' * 10000)]
    assert _find_turn_boundary(msgs, budget_tokens=0) == 2


def test_boundary_refuses_when_no_user_message():
    """Returns len() so the caller short-circuits instead of guessing."""
    msgs = [_a('a'), _a('b')]
    assert _find_turn_boundary(msgs) == len(msgs)


def test_boundary_honors_max_turns_cap():
    msgs = []
    for i in range(10):
        msgs += [_u(f't{i}'), _a(f'r{i}')]
    b = _find_turn_boundary(msgs, budget_tokens=float('inf'), max_turns=3)
    assert len(_user_indices_from(msgs, b)) == 3


def _user_indices_from(msgs, boundary):
    return [i for i, m in enumerate(msgs) if i >= boundary and m.get('role') == 'user']


def test_boundary_adds_older_turns_newest_first_until_budget():
    msgs = [_u('a'), _u('b'), _u('c')]
    # inf budget + cap 2 → exactly the two newest turns
    assert _find_turn_boundary(msgs, budget_tokens=float('inf'), max_turns=2) == 1


# ───────────────── shared cold/hot cut (both paths) ─────────────────

def test_split_cold_rounds_keeps_the_hot_tail():
    cold, hot = _split_cold_rounds([1, 2, 3, 4, 5], hot_rounds=2)
    assert (cold, hot) == ([1, 2, 3], [4, 5])


def test_split_cold_rounds_noop_when_nothing_to_fold():
    """Callers rely on `cold == []` to cheaply skip the whole fold."""
    assert _split_cold_rounds([1, 2], hot_rounds=2) == ([], [1, 2])
    assert _split_cold_rounds([], hot_rounds=3) == ([], [])


def test_split_cold_rounds_never_folds_everything():
    """hot_rounds<=0 must clamp to 1 — folding the newest round too would
    leave the model with zero verbatim tool context."""
    cold, hot = _split_cold_rounds([1, 2, 3], hot_rounds=0)
    assert len(hot) == 1 and cold == [1, 2]


def test_split_cold_rounds_is_index_space_agnostic():
    """Same policy object serves RAW toolRounds dicts and api-form spans."""
    raw = [{'toolCallId': 'a'}, {'toolCallId': 'b'}, {'toolCallId': 'c'}]
    spans = [(0, 2), (2, 4), (4, 6)]
    assert _split_cold_rounds(raw, 1)[1] == [{'toolCallId': 'c'}]
    assert _split_cold_rounds(spans, 1)[1] == [(4, 6)]


# ───────────────────── api-form round grouping ─────────────────────

def test_apiform_rounds_group_assistant_with_its_tool_results():
    msgs = [
        _u('go'),
        _a('', tool_calls=[{'id': '1'}]),
        {'role': 'tool', 'content': 'r1'},
        {'role': 'tool', 'content': 'r2'},
        _a('done'),
    ]
    assert _apiform_tool_rounds(msgs) == [(1, 4)]


def test_apiform_rounds_exclude_prose_and_user_rows():
    """Rows outside any span survive the fold untouched — the leading user
    turn and the model's reasoning must never be folded away."""
    msgs = [_u('go'), _a('thinking out loud'), {'role': 'system', 'content': 's'}]
    assert _apiform_tool_rounds(msgs) == []


def test_apiform_rounds_handles_back_to_back_rounds():
    msgs = [
        _a('', tool_calls=[{'id': '1'}]), {'role': 'tool', 'content': 'a'},
        _a('', tool_calls=[{'id': '2'}]), {'role': 'tool', 'content': 'b'},
    ]
    assert _apiform_tool_rounds(msgs) == [(0, 2), (2, 4)]


# ───────────────────── intra-turn fold ─────────────────────

def _giant_turn(n_rounds):
    msgs = [_u('one huge request')]
    for i in range(n_rounds):
        msgs.append(_a('', tool_calls=[{'id': str(i)}]))
        msgs.append({'role': 'tool', 'content': f'result {i}'})
    return msgs


def test_fold_never_orphans_a_tool_message():
    """An orphan `tool` row (no preceding assistant tool_calls) is a hard 400
    upstream. Whole-round folding is what prevents it."""
    kept, cold = _fold_recent_intra_turn(_giant_turn(6), hot_rounds=2)
    for i, m in enumerate(kept):
        if m.get('role') == 'tool':
            prev = kept[i - 1]
            assert prev.get('role') in ('assistant', 'tool')
            if prev.get('role') == 'assistant':
                assert prev.get('tool_calls')


def test_fold_keeps_leading_user_turn_and_hot_tail():
    kept, cold = _fold_recent_intra_turn(_giant_turn(6), hot_rounds=2)
    assert kept[0]['content'] == 'one huge request'
    assert _apiform_tool_rounds(kept) == [(1, 3), (3, 5)]
    # cold rounds are handed to the summarizer, never re-inserted verbatim
    assert [m for m in cold if m.get('role') == 'tool'][0]['content'] == 'result 0'


def test_fold_is_byte_identical_noop_for_normal_chats():
    """A conversation with <= hot_rounds tool rounds must be untouched, so
    ordinary chats near the window behave exactly as pre-fold."""
    msgs = _giant_turn(2)
    kept, cold = _fold_recent_intra_turn(msgs, hot_rounds=2)
    assert kept == msgs and cold == []


def test_fold_preserves_relative_order_of_cold_rounds():
    _kept, cold = _fold_recent_intra_turn(_giant_turn(5), hot_rounds=1)
    results = [m['content'] for m in cold if m.get('role') == 'tool']
    assert results == ['result 0', 'result 1', 'result 2', 'result 3']


def test_fold_partitions_without_loss_or_duplication():
    msgs = _giant_turn(5)
    kept, cold = _fold_recent_intra_turn(msgs, hot_rounds=2)
    assert len(kept) + len(cold) == len(msgs)


# ───────────────── spec coercion (real incident) ─────────────────

def test_coerce_spec_list_decodes_json_string_container():
    """Streamed tool calls sometimes record the array AS A STRING."""
    assert _coerce_spec_list('[{"path": "a.py"}]') == [{'path': 'a.py'}]


def test_coerce_spec_list_drops_truncated_string_instead_of_iterating_chars():
    """The "one letter per line" incident: iterating a raw string yields one
    char per element. Unparseable → [] so the caller emits nothing."""
    assert _coerce_spec_list('[{"path": "a.py", "end_line": 4]') == []
    assert _coerce_spec_list('') == []
    assert _coerce_spec_list('   ') == []


def test_coerce_spec_list_rejects_non_list_json():
    assert _coerce_spec_list('{"path": "a.py"}') == []
    assert _coerce_spec_list('42') == []


def test_coerce_spec_list_passes_real_lists_through():
    v = [{'path': 'a.py'}]
    assert _coerce_spec_list(v) == v
    assert _coerce_spec_list(None) == []


# ───────────────── recently-accessed files ─────────────────

def _call(name, args_json):
    return _a('', tool_calls=[{'function': {'name': name, 'arguments': args_json}}])


def test_recent_files_newest_first():
    msgs = [_call('write_file', '{"path": "old.py"}'),
            _call('write_file', '{"path": "new.py"}')]
    assert _extract_recently_accessed_files(msgs) == ['new.py', 'old.py']


def test_recent_files_dedupes_repeats():
    msgs = [_call('read_files', '{"reads": [{"path": "a.py"}]}'),
            _call('write_file', '{"path": "a.py"}')]
    assert _extract_recently_accessed_files(msgs) == ['a.py']


def test_recent_files_accepts_both_read_spec_shapes():
    """Opus emits reads=["a.py"]; others emit reads=[{"path": "a.py"}].
    Both are real full paths — a bare string element is NOT a stray char."""
    assert _extract_recently_accessed_files(
        [_call('read_files', '{"reads": ["a.py", {"path": "b.py"}]}')]
    ) == ['a.py', 'b.py']


def test_recent_files_reads_edits_arrays():
    msgs = [_call('apply_diffs', '{"edits": [{"path": "x.py"}, {"path": "y.py"}]}')]
    assert _extract_recently_accessed_files(msgs) == ['x.py', 'y.py']


def test_recent_files_survives_unparseable_and_non_dict_args():
    msgs = [_call('read_files', 'not json'),
            _call('write_file', '[1,2,3]'),
            _call('write_file', '{"path": "ok.py"}')]
    assert _extract_recently_accessed_files(msgs) == ['ok.py']


def test_recent_files_ignores_unrelated_tools():
    assert _extract_recently_accessed_files(
        [_call('web_search', '{"query": "x"}'), _call('run_command', '{"path": "p"}')]
    ) == []


def test_recent_files_never_emits_single_characters():
    """Regression guard for the reported garbage output: a string CONTAINER
    must not degrade into one path per character."""
    msgs = [_call('read_files', '{"reads": "[{\\"path\\": \\"a.py\\"]"}')]
    for p in _extract_recently_accessed_files(msgs):
        assert len(p) > 1
