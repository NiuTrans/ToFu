"""Engine tests for user-triggered manual compaction (§8 step 1).

Covers design-doc tests 1, 1b, 2, 3, 4, 7, 7b (docs/MANUAL_COMPACTION_DESIGN.md).
The load-bearing one is 1b: the raw-space index constraint — compaction of a
conversation whose old region contains a multi-``toolRounds`` assistant row must
NOT split a tool round (no orphan tool messages once rebuilt to api-form), and
the raw-aware token estimate must be much larger than the api-form-blind
``_estimate_msg_tokens`` on the same raw row.

Neuter validation (test_neuter_*): the guards are shown to have teeth — remove
the raw-space discipline and the boundary/estimate assertions FAIL.

Run:  python -B -m pytest -p no:napari tests/test_manual_compaction_engine.py
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fakes ────────────────────────────────────────────────────────────────

class _FakeStore:
    """In-memory ConversationStore double.  Records archive + CAS calls."""

    def __init__(self, messages, updated_at=1000):
        self.messages = list(messages)
        self.updated_at = updated_at
        self.archives = []          # list of (trigger, snapshot) in call order
        self.archive_summaries = {}
        self.cas_calls = []         # list of expected_updated_at seen
        self._next_archive_id = 100
        self.saved = None           # the messages passed to CAS (post-rewrite)
        self._fail_cas = False
        self.search_synced = False  # True iff the search-aware CAS path ran
        self.notified = False       # True iff conv-changed was pushed
        # Burst simulation: for the first N CAS attempts, a concurrent sibling
        # TAIL write lands right BEFORE our CAS (append a msg + bump updated_at),
        # so our CAS (on the reload-time updated_at) loses. After N it lands.
        self._races_remaining = 0
        self._race_seq = 0

    def load_conversation_messages(self, conv_id):
        return (list(self.messages), self.updated_at)

    def archive_transcript(self, conv_id, messages, *, trigger='force',
                           task_id='', round_num=0, model='',
                           tokens_before=0, tokens_after=0,
                           msgs_before=0, msgs_after=0, reason=''):
        aid = self._next_archive_id
        self._next_archive_id += 1
        # snapshot must be captured BEFORE any rewrite (test 3)
        self.archives.append((trigger, [dict(m) for m in messages]))
        return aid

    def prune_archives(self, conv_id, keep):
        return 0

    def update_archive_summary(self, archive_id, summary, tokens_after, msgs_after):
        self.archive_summaries[archive_id] = (summary, tokens_after, msgs_after)

    def cas_update_conversation_messages(self, conv_id, messages, expected_updated_at):
        self.cas_calls.append(expected_updated_at)
        if self._fail_cas:
            return 0
        # Burst: a sibling tail write lands just before this CAS, bumping
        # updated_at so the caller's expected value is now stale → 0 rows. The
        # caller must reload and retry; each retry sees the fresher tail.
        if self._races_remaining > 0:
            self._races_remaining -= 1
            self._race_seq += 1
            self.messages.append(_u(f'BURST tail {self._race_seq}', 90000 + self._race_seq))
            self.updated_at += 1
            return 0
        if expected_updated_at != self.updated_at:
            return 0
        self.messages = list(messages)
        self.saved = list(messages)
        self.updated_at += 1
        return 1

    def cas_sync_conversation_with_search(self, conv_id, messages, expected_updated_at):
        # CAS variant that ALSO refreshes msg_count + search_text + FTS. The
        # engine MUST use this (not the plain cas_update) because compaction
        # removes whole messages. Record that it was the path taken.
        self.search_synced = True
        return self.cas_update_conversation_messages(conv_id, messages, expected_updated_at)

    def notify_conversation_changed(self, conv_id):
        self.notified = True


def _install(monkeypatch, store, summary='COMPRESSED SUMMARY'):
    """Wire the fake store + a hermetic summary stub into _manual."""
    import lib.tasks_pkg.compaction._manual as man
    monkeypatch.setattr(man, 'get_conversation_store', lambda: store)
    # _archive_transcript imported into _manual — redirect it at the store so
    # we don't touch the DB, but STILL exercise _manual's own ordering.
    def _fake_archive(conv_id, messages, summary='', *, trigger='force',
                      task=None, round_num=0, tokens_before=0, tokens_after=0,
                      msgs_before=0, msgs_after=0, reason='', emit_event=True):
        return store.archive_transcript(
            conv_id, messages, trigger=trigger, tokens_before=tokens_before,
            msgs_before=msgs_before, reason=reason)
    monkeypatch.setattr(man, '_archive_transcript', _fake_archive)
    monkeypatch.setattr(man, '_generate_query_aware_summary',
                        lambda *a, **k: summary)
    monkeypatch.setattr(man, '_extract_recently_accessed_files', lambda m: [])
    return man


# ── Message builders (RAW storage shape) ───────────────────────────────────

def _u(text, ts):
    return {'role': 'user', 'content': text, 'timestamp': ts}


def _a_plain(text, ts):
    return {'role': 'assistant', 'content': text, 'timestamp': ts}


def _a_tools(final, n_rounds, ts, chars=4000):
    """RAW assistant row carrying N complete toolRounds (as persisted)."""
    rounds = []
    for i in range(n_rounds):
        rounds.append({
            'toolCallId': f'tc_{ts}_{i}',
            'toolName': 'read_files',
            'status': 'done',
            'toolArgs': '{"path": "x"}',
            'toolContent': 'RESULT ' + ('x' * chars),
            'llmRound': i,
            'assistantContent': 'let me read' if i == 0 else '',
        })
    return {'role': 'assistant', 'content': final, 'timestamp': ts,
            'toolRounds': rounds}


def _long_conv(n_turns=20):
    """A conversation with many turns (> _MAX_PRESERVE_TURNS so the default
    preserve window is genuinely exceeded).  A heavy multi-``toolRounds``
    assistant sits at turn ``n_turns-2`` so, with a small ``keep_recent_turns``,
    it lands inside the RESERVE region — proving a preserved tool round stays
    intact after the rewrite (test 1b part c)."""
    heavy_turn = n_turns - 2
    msgs = [_u('原始目标：修复登录 bug', 1000)]           # objective anchor (turn 0)
    msgs.append(_a_plain('好的，我来看看', 1001))
    for t in range(1, n_turns):
        msgs.append(_u(f'第 {t} 步继续', 1000 + t * 10))
        if t == heavy_turn:
            msgs.append(_a_tools(f'完成第 {t} 步', n_rounds=8, ts=1000 + t * 10 + 1))
        else:
            msgs.append(_a_plain(f'完成第 {t} 步 ' + ('y' * 200), 1000 + t * 10 + 1))
    return msgs


def _api_ok(api_msgs):
    """True if the api-form list has no orphan tool messages and every
    assistant tool_call has a matching tool result."""
    open_ids = set()
    for m in api_msgs:
        if m.get('role') == 'assistant' and m.get('tool_calls'):
            for tc in m['tool_calls']:
                open_ids.add(tc['id'])
        elif m.get('role') == 'tool':
            tcid = m.get('tool_call_id')
            if tcid not in open_ids:
                return False, f'orphan tool result {tcid}'
            open_ids.discard(tcid)
    return True, ''


# ═══════════════════════════════════════════════════════════════════════════
#  Test 1 — persistence: DB messages replaced, msgs_after < msgs_before
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_manual_compaction_persists_summary(monkeypatch):
    store = _FakeStore(_long_conv())
    man = _install(monkeypatch, store)
    before = len(store.messages)

    res = man.compact_conversation_now('conv1', config={}, task={'convId': 'conv1'})

    assert res['ok'] is True
    assert store.saved is not None, 'CAS write must have happened'
    # Must use the search-aware CAS path (refreshes msg_count+search_text+FTS)
    # because compaction removes whole messages; a plain cas_update would leave
    # the sidebar count stale and search matching compacted-away text.
    assert store.search_synced is True, 'must persist via cas_sync_conversation_with_search'
    assert store.notified is True, 'must notify conv-changed after a landed write'
    assert res['msgsAfter'] < res['msgsBefore'] == before
    # summary message present, exactly one
    summaries = [m for m in store.messages if m.get('_isCompactionSummary')]
    assert len(summaries) == 1
    assert man._SUMMARY_HEADER in summaries[0]['content']
    assert summaries[0].get('_compactionArchiveId') is not None
    assert summaries[0].get('_estimatedPromptTokens') == res['tokensAfter']


# ═══════════════════════════════════════════════════════════════════════════
#  Test 1b — RAW-SPACE constraint: never split a tool round; raw estimate
#            >> api-form-blind _estimate_msg_tokens on the same raw row.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_raw_turn_boundary_never_splits_tool_round(monkeypatch):
    from lib.tasks_pkg.compaction import _manual as man
    from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens
    from lib.tasks_pkg.conv_message_builder import _transform_messages

    raw = _long_conv()

    # (a) boundary lands on a user index in the RAW list.
    b = man._raw_turn_boundary(raw, config={}, task={'convId': 'c'})
    assert raw[b].get('role') == 'user', f'boundary {b} not on a user row'

    # (b) raw-aware estimate of the heavy tool turn is MUCH larger than the
    #     api-form-blind _estimate_msg_tokens applied to the raw assistant row.
    heavy = _a_tools('done', n_rounds=8, ts=5, chars=4000)
    raw_est = man._raw_estimate_tokens([heavy], config={})
    blind_est = _estimate_msg_tokens(heavy)          # blind to toolRounds
    assert raw_est > blind_est * 5, (
        f'raw-aware estimate {raw_est} should dwarf blind {blind_est} '
        '(toolRounds payload must be counted)')

    # (c) compact, then rebuild to api-form: no orphan tool / split round.
    store = _FakeStore(raw)
    man2 = _install(monkeypatch, store)
    res = man2.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is True
    api = _transform_messages(store.messages, {})
    ok, why = _api_ok(api)
    assert ok, f'tool round split after manual compaction: {why}'


# ═══════════════════════════════════════════════════════════════════════════
#  Test 2 — objective anchor preserved verbatim, before the summary
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_manual_compaction_preserves_objective_anchor(monkeypatch):
    store = _FakeStore(_long_conv())
    man = _install(monkeypatch, store)
    anchor_text = store.messages[0]['content']

    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is True

    roles = [m.get('role') for m in store.messages]
    # first live row is the verbatim anchor user message
    assert store.messages[0]['content'] == anchor_text
    assert store.messages[0].get('role') == 'user'
    # summary comes AFTER the anchor
    summary_idx = next(i for i, m in enumerate(store.messages)
                       if m.get('_isCompactionSummary'))
    assert summary_idx > 0
    # anchor text appears exactly once (not duplicated by compaction)
    n_anchor = sum(1 for m in store.messages
                   if m.get('role') == 'user' and m.get('content') == anchor_text)
    assert n_anchor == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Test 3 — archive the full pre-compaction snapshot BEFORE rewriting
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_manual_compaction_archives_before_rewrite(monkeypatch):
    store = _FakeStore(_long_conv())
    man = _install(monkeypatch, store)
    original = [dict(m) for m in store.messages]

    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is True

    assert len(store.archives) == 1
    trigger, snapshot = store.archives[0]
    assert trigger == 'manual'
    # snapshot is the FULL pre-compaction message list (not the rewritten one)
    assert len(snapshot) == len(original)
    assert snapshot[0]['content'] == original[0]['content']
    # and the archive summary was filled in after the rewrite
    assert res['archiveId'] in store.archive_summaries


# ═══════════════════════════════════════════════════════════════════════════
#  Speed — do NOT re-project the WHOLE conversation to api-form more than once.
#
#  `_transform_messages` (the raw→api projection) is the dominant CPU cost of a
#  manual /compact on a multi-MB conversation. The engine used to project the
#  ENTIRE raw list three separate times (route `tokens_before`, plan-floor
#  `total_tokens`, and `_extract_recently_accessed_files`). Those are redundant:
#  one whole-conversation projection can feed all of them. This guard counts
#  full-conversation projections and fails if the engine regresses to >1.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_manual_compaction_projects_whole_conv_at_most_once(monkeypatch):
    import lib.tasks_pkg.conv_message_builder as cmb
    store = _FakeStore(_long_conv())
    man = _install(monkeypatch, store)
    full_len = len(store.messages)

    real_transform = cmb._transform_messages
    whole_conv_projections = {'n': 0}

    def _counting_transform(msgs, cfg):
        # A "whole-conversation" projection is one whose input length equals the
        # full stored message count — the expensive case we must not repeat.
        if isinstance(msgs, list) and len(msgs) >= full_len:
            whole_conv_projections['n'] += 1
        return real_transform(msgs, cfg)

    monkeypatch.setattr(cmb, '_transform_messages', _counting_transform)

    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is True
    assert whole_conv_projections['n'] <= 1, (
        f"manual compaction projected the whole conversation "
        f"{whole_conv_projections['n']}× — must reuse a single projection")


# ═══════════════════════════════════════════════════════════════════════════
#  Test 4 — idempotence: compacting an already-compacted conv stays valid
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_manual_compaction_idempotent(monkeypatch):
    store = _FakeStore(_long_conv())
    man = _install(monkeypatch, store)
    anchor_text = store.messages[0]['content']

    r1 = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert r1['ok'] is True
    first_summary_count = sum(1 for m in store.messages
                              if m.get('_isCompactionSummary'))

    # add a couple more turns so there is again something to compact
    store.messages.append(_u('新的一步', 9000))
    store.messages.append(_a_plain('done ' + ('z' * 400), 9001))
    store.messages.append(_u('再一步', 9010))
    store.messages.append(_a_plain('done ' + ('z' * 400), 9011))

    r2 = man.compact_conversation_now('c', config={}, task={'convId': 'c'},
                                      keep_recent_turns=1)
    assert r2['ok'] is True
    # anchor still appears exactly once; structure intact
    n_anchor = sum(1 for m in store.messages
                   if m.get('role') == 'user' and m.get('content') == anchor_text)
    assert n_anchor == 1, 'anchor must not be duplicated across compactions'
    assert first_summary_count == 1


# ═══════════════════════════════════════════════════════════════════════════
#  Test 7 — summary survives the api rebuild as a plain assistant text
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_summary_message_survives_api_rebuild(monkeypatch):
    from lib.tasks_pkg.conv_message_builder import _transform_messages
    store = _FakeStore(_long_conv())
    man = _install(monkeypatch, store)
    man.compact_conversation_now('c', config={}, task={'convId': 'c'})

    api = _transform_messages(store.messages, {})
    ok, why = _api_ok(api)
    assert ok, why
    # summary body survives as plain assistant text
    joined = '\n'.join(m.get('content', '') for m in api
                       if m.get('role') == 'assistant'
                       and isinstance(m.get('content'), str))
    assert man._SUMMARY_HEADER in joined


# ═══════════════════════════════════════════════════════════════════════════
#  Test 7b — reserve region starting with an assistant must NOT let the
#            summary be merged into it by _merge_consecutive_same_role.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_summary_reserve_join_no_double_assistant(monkeypatch):
    from lib.tasks_pkg.conv_message_builder import _transform_messages
    # Because the boundary always lands on a user index, the reserve region
    # begins with a user row → summary (assistant) is always followed by user.
    store = _FakeStore(_long_conv())
    man = _install(monkeypatch, store)
    man.compact_conversation_now('c', config={}, task={'convId': 'c'})

    summary_idx = next(i for i, m in enumerate(store.messages)
                       if m.get('_isCompactionSummary'))
    # the row AFTER the summary is a user row (raw shape)
    assert summary_idx + 1 < len(store.messages)
    assert store.messages[summary_idx + 1].get('role') == 'user'

    # and after api rebuild the summary text is NOT fused with a later
    # assistant turn (it stands as its own assistant message).
    api = _transform_messages(store.messages, {})
    summary_msgs = [m for m in api
                    if m.get('role') == 'assistant'
                    and isinstance(m.get('content'), str)
                    and man._SUMMARY_HEADER in m['content']]
    assert len(summary_msgs) == 1
    # the fused-in check: the summary message content must not also contain a
    # later reserve assistant's body (they'd only fuse if adjacent same-role).
    assert '完成第 9 步' not in summary_msgs[0]['content']


# ═══════════════════════════════════════════════════════════════════════════
#  nothing-to-compact guard
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_manual_compaction_nothing_to_compact(monkeypatch):
    store = _FakeStore([_u('只有一轮', 1), _a_plain('答复', 2)])
    man = _install(monkeypatch, store)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is False and res['error'] == 'nothing_to_compact'
    assert store.saved is None, 'must not write when nothing to compact'
    assert store.archives == [], 'must not archive when nothing to compact'


@pytest.mark.unit
def test_manual_compaction_stale_cas_aborts(monkeypatch):
    # A CAS that NEVER lands (folded region unchanged, but every write races)
    # must exhaust the reconcile budget → stale. Use a tiny budget so the test
    # doesn't spin for the full default (3s).
    monkeypatch.setenv('TOFU_MANUAL_RECONCILE_BUDGET_SEC', '0.05')
    store = _FakeStore(_long_conv())
    store._fail_cas = True
    man = _install(monkeypatch, store)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is False and res['error'] == 'stale'
    assert store.cas_calls, 'must have attempted at least one CAS before giving up'


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER validation — prove the raw-space guards have teeth.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_neuter_blind_estimate_would_undercount():
    """If _raw_estimate_tokens were (wrongly) implemented with the api-form
    -blind _estimate_msg_tokens on raw rows, a heavy toolRounds turn would be
    massively under-counted. This test documents the gap the raw-aware
    estimate closes — it FAILS the '>5x' bar test 1b relies on."""
    from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens
    from lib.tasks_pkg.compaction import _manual as man

    heavy = _a_tools('done', n_rounds=8, ts=5, chars=4000)
    raw_aware = man._raw_estimate_tokens([heavy], config={})
    blind = _estimate_msg_tokens(heavy)
    # The neuter (blind) path is the WRONG one; assert it truly under-counts,
    # which is exactly why _raw_estimate_tokens must project first.
    assert blind * 5 < raw_aware
    assert blind < 200, f'blind estimate {blind} should miss the toolRounds payload'


def _early_heavy_conv(n_turns=20):
    """Fixture with heavy multi-``toolRounds`` turns EARLY (before the preserve
    window), so api-form projection inflates absolute indices relative to raw —
    making the api-space vs raw-space boundary divergence deterministic."""
    msgs = [_u('原始目标', 1000), _a_plain('ok', 1001)]
    for t in range(1, n_turns):
        msgs.append(_u(f'第 {t} 步', 1000 + t * 10))
        if t <= 5:                       # heavy tool turns near the start
            msgs.append(_a_tools(f'done {t}', n_rounds=6, ts=1000 + t * 10 + 1))
        else:
            msgs.append(_a_plain(f'done {t} ' + ('y' * 200), 1000 + t * 10 + 1))
    return msgs


@pytest.mark.unit
def test_neuter_apiform_boundary_would_split():
    """Simulate the BUG: compute the boundary in api-form index space, then use
    that index to slice the RAW list (the mixed-space mistake §4.1 forbids).

    Because projection expands each early tool turn into many api messages, the
    api-space boundary is a LARGER absolute index than the correct raw-space
    boundary.  Applied to the shorter raw list it cuts too far → the preserved
    reserve is mis-aligned (does not start on a user row) and/or preserves the
    WRONG turns.  The correct raw-space boundary does neither.  This proves the
    discipline is load-bearing, not decorative."""
    from lib.tasks_pkg.compaction import _manual as man
    from lib.tasks_pkg.compaction._layer2 import _find_turn_boundary
    from lib.tasks_pkg.conv_message_builder import _transform_messages

    raw = _early_heavy_conv()

    # CORRECT: boundary in raw space (starts reserve on a user row, keeps 3).
    raw_b = man._raw_turn_boundary(raw, config={}, task={'c': 1},
                                   budget_tokens=1, max_turns=3)
    assert raw[raw_b].get('role') == 'user'
    correct_reserve = raw[raw_b:]

    # BUGGY: boundary in api space, sliced onto the RAW list.
    api = _transform_messages(list(raw), {})
    api_b = _find_turn_boundary(api, budget_tokens=1, max_turns=3)
    bad_reserve = raw[api_b:]

    # (1) The two index spaces genuinely diverge — the entire reason you may
    #     not compute in one space and slice in the other.
    assert api_b != raw_b, (
        'api and raw boundary coincided — early tool expansion should inflate '
        'the api index')
    # (2) The mixed-space slice is demonstrably wrong: it does not start on a
    #     user row (mid-turn cut) and/or preserves a different reserve than the
    #     correct raw-space computation.
    bad_starts_on_user = bool(bad_reserve) and bad_reserve[0].get('role') == 'user'
    assert (not bad_starts_on_user) or (len(bad_reserve) != len(correct_reserve)), (
        'the api-space boundary must mis-slice the raw list')


# ═══════════════════════════════════════════════════════════════════════════
#  档B — intra-turn folding of a SINGLE giant turn (the 422 nothing_to_compact
#        bug: one user request answered with dozens of tool rounds fills the
#        window, but the turn-based boundary always preserves the current turn
#        WHOLE, so the old plan_manual_compaction refused it → HTTP 422).
# ═══════════════════════════════════════════════════════════════════════════

def _giant_turn_conv(n_rounds=40, chars=4000):
    """A conversation that is ONE user turn answered by ONE assistant carrying
    many ``toolRounds``.  There is no old region (boundary preserves the sole
    turn whole), so ONLY 档B intra-turn folding can compact it."""
    return [
        _u('修复登录 bug，尽可能彻底', 1000),
        _a_tools('已完成，详见上面各步', n_rounds=n_rounds, ts=1001, chars=chars),
    ]


@pytest.mark.unit
def test_intra_turn_folds_single_giant_turn(monkeypatch):
    """POSITIVE guard for 档B: a single giant tool turn is compactable via
    intra-turn folding (mode='intra_turn'), cold rounds are folded out, the
    rebuilt api-form has NO orphan tool, and tokens drop significantly."""
    from lib.tasks_pkg.compaction import _manual as man
    from lib.tasks_pkg.compaction._constants import (
        _MANUAL_INTRA_TURN_HOT_ROUNDS, _MANUAL_COMPACT_MIN_TOKENS)
    from lib.tasks_pkg.conv_message_builder import _transform_messages

    raw = _giant_turn_conv(n_rounds=40)

    # Sanity: this fixture is above the floor (so the ONLY reason to refuse
    # would be the removed turn-based short-circuit, not the size floor).
    total = man._raw_estimate_tokens(raw, config={})
    assert total >= _MANUAL_COMPACT_MIN_TOKENS, (
        f'fixture {total} tok must exceed the floor {_MANUAL_COMPACT_MIN_TOKENS}')

    # (a) plan is intra_turn (NOT None → NOT the 422 nothing_to_compact bug).
    plan = man.plan_manual_compaction(raw, config={}, task={'convId': 'c'})
    assert plan is not None, 'giant single turn must be compactable (档B)'
    assert plan['mode'] == 'intra_turn'
    assert plan['old_raw'] == [], 'no old region for a single turn'

    # (b) _collect_reserve_folds actually folds the giant turn: it keeps the
    #     hot tail verbatim and marks the older rounds as cold.
    folds = plan['intra_folds']
    assert len(folds) == 1, 'the one giant assistant must be folded'
    fold = folds[0]
    assert len(fold['hot_rounds']) == _MANUAL_INTRA_TURN_HOT_ROUNDS
    assert len(fold['cold_rounds']) == 40 - _MANUAL_INTRA_TURN_HOT_ROUNDS
    # cold + hot together == the original rounds, in order (nothing lost/added)
    assert fold['cold_rounds'] + fold['hot_rounds'] == raw[1]['toolRounds']

    # (c) full engine run: tokens drop significantly + NO orphan tool.
    store = _FakeStore(raw)
    man2 = _install(monkeypatch, store)
    res = man2.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is True, res
    assert res['tokensAfter'] < res['tokensBefore'] * 0.5, (
        f"intra-turn fold must cut tokens hard: "
        f"{res['tokensBefore']} → {res['tokensAfter']}")
    # the preserved giant assistant now carries only the hot tail
    heavy = [m for m in store.messages
             if m.get('role') == 'assistant' and m.get('toolRounds')]
    assert len(heavy) == 1
    assert len(heavy[0]['toolRounds']) == _MANUAL_INTRA_TURN_HOT_ROUNDS
    assert heavy[0].get('_intraTurnFolded') == 40 - _MANUAL_INTRA_TURN_HOT_ROUNDS
    # rebuild to api-form: dropping WHOLE cold rounds must not orphan a tool
    api = _transform_messages(store.messages, {})
    ok, why = _api_ok(api)
    assert ok, f'intra-turn fold split a tool round: {why}'


@pytest.mark.unit
def test_neuter_old_shortcircuit_would_refuse_giant_turn():
    """NEUTER negative control for 档B, faithful to the exact pre-fix code.

    The old ``plan_manual_compaction`` short-circuited with
    ``if boundary <= system_end: return None`` — for a single giant turn the
    boundary IS ``system_end`` (nothing before the sole user row), so the old
    path returned None → the route mapped it to 422 nothing_to_compact.

    Re-run that removed decision on the same giant-turn fixture and assert it
    would have refused.  If 档B is reverted, the positive test above regresses
    to exactly this behaviour — so this pins the fix as load-bearing."""
    from lib.tasks_pkg.compaction import _manual as man

    raw = _giant_turn_conv(n_rounds=40)

    system_end = man._system_end(raw)
    boundary = man._raw_turn_boundary(raw, config={}, task={'convId': 'c'})

    # The removed short-circuit: a single preserved turn → boundary==system_end.
    assert boundary <= system_end, (
        'single giant turn: boundary must collapse onto system_end (this is '
        'exactly why the old code refused it)')
    old_plan_would_be = None if boundary <= system_end else 'turns'
    assert old_plan_would_be is None, (
        'the OLD turn-based short-circuit refuses the giant turn (the 422 bug)')

    # And the CURRENT code does the opposite on the identical input — proving
    # the guard has teeth (revert 档B ⇒ this diverges back to None).
    new_plan = man.plan_manual_compaction(raw, config={}, task={'convId': 'c'})
    assert new_plan is not None and new_plan['mode'] == 'intra_turn'


# ═══════════════════════════════════════════════════════════════════════════
#  RECONCILE — the "manual /compact always 409 on an active conversation" fix.
#
#  Root cause: the summary LLM call takes seconds; a sibling agent turn writes
#  the conversation TAIL meanwhile, bumping updated_at. The old single-shot CAS
#  (on the LOAD-time updated_at, guarding the WHOLE row) lost that race even
#  though the concurrent write only APPENDED to the preserved region and never
#  touched the folded (summarized) region. The fix: reload after summarizing,
#  verify the folded prefix is byte-unchanged, rebuild over the CURRENT tail,
#  CAS on the CURRENT updated_at. Only a change WITHIN the folded region is a
#  true conflict → stale.
# ═══════════════════════════════════════════════════════════════════════════

def _install_with_concurrent_write(monkeypatch, store, mutate, summary='COMPRESSED SUMMARY'):
    """Like _install, but the summary stub ALSO runs ``mutate(store)`` — i.e. a
    sibling agent's concurrent write lands DURING the (slow) summary call,
    exactly reproducing the production 35s-window race."""
    man = _install(monkeypatch, store, summary=summary)

    def _summary_then_write(*a, **k):
        mutate(store)
        return summary
    monkeypatch.setattr(man, '_generate_query_aware_summary', _summary_then_write)
    return man


@pytest.mark.unit
def test_reconcile_preserves_tail_appended_during_summary(monkeypatch):
    """A sibling agent appends N new tail turns DURING the summary window. The
    compaction must SUCCEED (not 409), keep ALL N appended messages in the
    preserved region, and still fold the old region into a summary."""
    from lib.tasks_pkg.conv_message_builder import _transform_messages

    store = _FakeStore(_long_conv())
    anchor_text = store.messages[0]['content']

    appended = [
        _u('SIBLING: 新的一步 A', 99000),
        _a_tools('sibling done A', n_rounds=2, ts=99001, chars=500),
        _u('SIBLING: 新的一步 B', 99010),
        _a_plain('sibling done B ' + ('q' * 300), 99011),
    ]

    def _mutate(st):
        # Sibling writes the TAIL and bumps updated_at (as a real PATCH does).
        st.messages.extend([dict(m) for m in appended])
        st.updated_at += 1

    man = _install_with_concurrent_write(monkeypatch, store, _mutate)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})

    # (1) SUCCESS — not the stale/409 the old single-shot CAS produced.
    assert res['ok'] is True, f'reconcile must succeed, got {res}'
    assert store.search_synced is True and store.notified is True

    # (2) ALL sibling-appended messages survive in the preserved region.
    contents = [m.get('content', '') for m in store.messages]
    assert 'SIBLING: 新的一步 A' in contents
    assert 'SIBLING: 新的一步 B' in contents
    sib_tools = [m for m in store.messages
                 if m.get('role') == 'assistant' and m.get('content') == 'sibling done A']
    assert len(sib_tools) == 1 and len(sib_tools[0].get('toolRounds', [])) == 2, (
        'sibling tool turn appended during summary must be preserved intact')

    # (3) the old region is still folded into exactly one summary, anchor kept.
    summaries = [m for m in store.messages if m.get('_isCompactionSummary')]
    assert len(summaries) == 1
    assert store.messages[0].get('content') == anchor_text
    assert res['msgsAfter'] < res['msgsBefore']

    # (4) rebuild to api-form: no orphan tool / split round.
    api = _transform_messages(store.messages, {})
    ok, why = _api_ok(api)
    assert ok, f'reconciled rebuild split a tool round: {why}'


@pytest.mark.unit
def test_reconcile_conflict_in_folded_region_aborts_stale(monkeypatch):
    """If the concurrent write touches the FOLDED region (a message the summary
    consumed), that is a REAL conflict — the summary no longer faithfully
    represents what it replaced — so the engine must still return stale."""
    store = _FakeStore(_long_conv())

    def _mutate(st):
        # Rewrite an OLD (folded) message — index 1 is well before any boundary.
        st.messages[1] = _a_plain('MUTATED OLD CONTENT under us', 1001)
        st.updated_at += 1

    man = _install_with_concurrent_write(monkeypatch, store, _mutate)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})

    assert res['ok'] is False and res['error'] == 'stale', (
        f'a folded-region rewrite is a real conflict → stale, got {res}')
    # archive kept (harmless), but the live conversation was NOT overwritten
    assert res.get('archiveId') is not None
    assert not any(m.get('_isCompactionSummary') for m in store.messages), (
        'must not persist a summary when the folded region changed under us')


@pytest.mark.unit
def test_neuter_without_reconcile_tail_append_would_409(monkeypatch):
    """NEUTER: prove the reconcile is load-bearing. On the SAME tail-append
    scenario, a single-shot CAS on the LOAD-time updated_at (the pre-fix path)
    necessarily fails — demonstrating that removing reconcile regresses to the
    guaranteed-409 behaviour the user reported."""
    store = _FakeStore(_long_conv())
    load_time_updated_at = store.updated_at

    # Simulate the sibling tail write that happens during the summary window.
    store.messages.extend([_u('SIBLING tail', 99000),
                           _a_plain('sibling done', 99001)])
    store.updated_at += 1

    # The OLD code CAS-ed on the LOAD-time updated_at → 0 rows (lost race).
    affected = store.cas_sync_conversation_with_search(
        'c', list(store.messages), load_time_updated_at)
    assert affected == 0, (
        'pre-fix single-shot CAS on the stale load-time updated_at MUST lose '
        'the race — this is exactly the 409 the reconcile loop eliminates')

    # And the CURRENT engine, on an equivalent fresh scenario, succeeds.
    store2 = _FakeStore(_long_conv())

    def _mutate(st):
        st.messages.extend([_u('SIBLING tail', 99000),
                           _a_plain('sibling done', 99001)])
        st.updated_at += 1

    man = _install_with_concurrent_write(monkeypatch, store2, _mutate)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is True, (
        f'reconcile must turn the same race into a success, got {res}')


# ═══════════════════════════════════════════════════════════════════════════
#  TIME-BUDGET reconcile — a sustained write BURST (K races > any fixed retry
#  count) must still land within the wall-clock budget, not surface as 409.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_reconcile_survives_write_burst_beyond_fixed_count(monkeypatch):
    """The regression the time-budget closes: a burst of K tail writes — where
    K exceeds any small fixed retry count — must NOT produce a 409. Each pass
    loses the CAS to a fresh tail write; the loop keeps reconciling against the
    ever-fresher tail until it lands (within budget)."""
    from lib.tasks_pkg.conv_message_builder import _transform_messages

    # Generous budget; K races far exceeds the old fixed cap (2 → 3 passes).
    monkeypatch.setenv('TOFU_MANUAL_RECONCILE_BUDGET_SEC', '5.0')
    K = 12
    store = _FakeStore(_long_conv())
    store._races_remaining = K

    man = _install(monkeypatch, store)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})

    # (1) SUCCESS despite K races well beyond any fixed retry count.
    assert res['ok'] is True, f'burst of {K} must still land within budget, got {res}'
    # (2) it really did retry more than a small fixed count would allow.
    assert len(store.cas_calls) >= K + 1, (
        f'expected > {K} CAS attempts (each burst write forces a retry), '
        f'got {len(store.cas_calls)}')
    # (3) every BURST tail write injected during the window is preserved.
    contents = [m.get('content', '') for m in store.messages]
    for i in range(1, K + 1):
        assert f'BURST tail {i}' in contents, f'BURST tail {i} lost'
    # (4) exactly one summary, structure intact (no orphan tool).
    summaries = [m for m in store.messages if m.get('_isCompactionSummary')]
    assert len(summaries) == 1
    api = _transform_messages(store.messages, {})
    ok, why = _api_ok(api)
    assert ok, f'burst reconcile split a tool round: {why}'


@pytest.mark.unit
def test_neuter_fixed_count_would_409_on_burst(monkeypatch):
    """NEUTER: prove the TIME budget (not a fixed count) is what closes the
    burst hole. Re-simulate the OLD fixed-count policy (cap=2 → 3 passes) on a
    burst of K=12 races and assert it would have surfaced a 409 — exactly the
    residual failure the time budget eliminates."""
    OLD_FIXED_CAP = 2          # the pre-fix _MANUAL_RECONCILE_MAX_RETRIES
    K = 12
    store = _FakeStore(_long_conv())
    store._races_remaining = K

    # Replay the OLD loop shape (fixed attempt count) against the same store.
    attempts = 0
    landed = False
    while True:
        attempts += 1
        # each pass: a burst write lands pre-CAS, so cas fails until races drain
        affected = store.cas_update_conversation_messages(
            'c', list(store.messages), store.updated_at - 1)  # deliberately stale
        if affected:
            landed = True
            break
        if attempts > OLD_FIXED_CAP:      # old: give up after cap → 409
            break
    assert not landed and attempts == OLD_FIXED_CAP + 1, (
        'the OLD fixed-count policy exhausts its 3 passes on a 12-race burst '
        'and 409s — this is exactly the hole the time budget closes')

    # And the CURRENT time-budget engine lands on an equivalent burst.
    monkeypatch.setenv('TOFU_MANUAL_RECONCILE_BUDGET_SEC', '5.0')
    store2 = _FakeStore(_long_conv())
    store2._races_remaining = K
    man = _install(monkeypatch, store2)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is True, (
        f'time-budget engine must land the same {K}-race burst, got {res}')


@pytest.mark.unit
def test_reconcile_budget_exhaustion_is_stale(monkeypatch):
    """When the budget genuinely runs out (writes never stop), the engine
    returns stale rather than spinning forever — the defensive backstop."""
    monkeypatch.setenv('TOFU_MANUAL_RECONCILE_BUDGET_SEC', '0.05')
    store = _FakeStore(_long_conv())
    # Never-ending burst: always races, never lands.
    store._races_remaining = 10 ** 9

    man = _install(monkeypatch, store)
    res = man.compact_conversation_now('c', config={}, task={'convId': 'c'})
    assert res['ok'] is False and res['error'] == 'stale'
    assert len(store.cas_calls) >= 1, 'must attempt at least one CAS'
    # NOT the infinite-loop guard — the time budget stopped it well under the cap.
    from lib.tasks_pkg.compaction._constants import _MANUAL_RECONCILE_HARD_ITER_CAP
    assert len(store.cas_calls) < _MANUAL_RECONCILE_HARD_ITER_CAP, (
        'time budget must stop the loop long before the infinite-loop iter cap')
