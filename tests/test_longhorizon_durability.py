"""tests/test_longhorizon_durability.py — year-scale run correctness.

Covers the three mechanisms that keep a very-long-horizon autonomous run
correct, each with a load-bearing neuter:

  #3 OBJECTIVE ANCHOR (the correctness centerpiece) — the first real user
     message (the north-star objective) survives N successive L2 compactions
     VERBATIM, and even a last-resort L3 head-truncate never drops it.  Without
     the anchor it would be fed to the lossy summarizer every pass
     (summary-of-summary drift) and could be truncated away entirely.

  #1 autopilotSummaries retention — the per-run close-out map is capped so a
     year-scale conversation's settings JSON can't grow unbounded (it
     re-serializes into every settings PUT + IDB write).

  #2 transcript_archive retention — the ring-buffer prune keeps only the newest
     N raw transcripts per conversation.

Holes A & B (owner-flagged): the objective anchor is idempotent (present
exactly once, byte-identical across compactions — no unbounded prefix growth),
and the autopilot objective pin is durable across run boundaries (not wiped on
conclude), so a re-scan can't snap it to a stale ancient turn.
"""

import pytest

import lib.tasks_pkg.compaction._layer2._compact as l2
import lib.tasks_pkg.compaction._reactive as reactive


# ══════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════

OBJECTIVE = 'BUILD THE YEARLY REPORT PIPELINE end to end and keep it correct'


def _sys():
    return {'role': 'system', 'content': 'you are a coding assistant'}


def _user(text):
    return {'role': 'user', 'content': text}


def _assistant(text):
    return {'role': 'assistant', 'content': text}


def _build_long_conversation(n_turns):
    """system + objective(user) + n_turns of (assistant, user) filler."""
    msgs = [_sys(), _user(OBJECTIVE)]
    for i in range(n_turns):
        msgs.append(_assistant(f'assistant work step {i} doing filler stuff '
                               f'that is long enough to matter {"x" * 200}'))
        msgs.append(_user(f'follow-up instruction number {i} continue please'))
    return msgs


@pytest.fixture
def fake_summary(monkeypatch):
    """Replace the LLM summary with a deterministic marker that PARAPHRASES —
    it never echoes the objective text verbatim, so if the anchor mechanism
    fails, the objective is provably gone from the live context."""
    def _fake(
        old_messages,
        current_query,
        log_prefix='',
        conv_id='',
        task=None,
        usage_out=None,
        anchor_text='',
    ):
        return ('### 1. Primary Request\n[summary of earlier work — '
                'objective paraphrased, not verbatim]\n### 6. All User Messages\n'
                '(compressed)')
    monkeypatch.setattr(l2, '_generate_query_aware_summary', _fake)
    # Neutralize archival + cooldown side effects for a pure unit test.
    monkeypatch.setattr(l2, '_archive_transcript', lambda *a, **k: None)
    return _fake


def _objective_present_verbatim(messages):
    """True iff the exact objective text survives as a standalone user msg."""
    for m in messages:
        if m.get('role') == 'user' and m.get('content') == OBJECTIVE:
            return True
    return False


# ══════════════════════════════════════════════════════════
#  #3 — objective anchor helper
# ══════════════════════════════════════════════════════════

def test_anchor_index_is_first_real_user_message():
    msgs = [_sys(), _sys(), _user(OBJECTIVE), _assistant('a'), _user('later')]
    assert l2._objective_anchor_index(msgs) == 2


def test_anchor_index_skips_vu_and_empty():
    msgs = [_sys(),
            {'role': 'user', 'content': '  ', '_isVirtualUser': False},
            {'role': 'user', 'content': 'real goal here', '_isVuDirective': True},
            _user(OBJECTIVE)]
    # empty user + VU directive skipped → the real objective is index 3
    assert l2._objective_anchor_index(msgs) == 3


def test_anchor_index_none_when_no_user():
    assert l2._objective_anchor_index([_sys(), _assistant('hi')]) is None


def test_anchor_index_skips_ismeta_context_carrier():
    """The builder prepends a synthetic ``_isMeta`` user message (CLAUDE.md /
    preference carrier) BEFORE the real user turn. The anchor must protect the
    human's objective, not that injected carrier — same skip as autopilot's
    ``_extract_objective`` (ONE notion of objective, not parallel)."""
    msgs = [_sys(),
            {'role': 'user', 'content': 'CLAUDE.md project intel',
             '_isMeta': True},
            _user(OBJECTIVE)]
    assert l2._objective_anchor_index(msgs) == 2


def test_NC_anchor_ismeta_skip_is_load_bearing(monkeypatch):
    """NEUTER: if the anchor did NOT skip ``_isMeta`` it would return the
    injected carrier's index (1) instead of the human objective (2)."""
    import lib.tasks_pkg.compaction._layer2._anchor as _l2mod
    src = _l2mod._objective_anchor_index.__doc__  # sanity: fn exists
    assert src is not None
    msgs = [_sys(),
            {'role': 'user', 'content': 'injected carrier', '_isMeta': True},
            _user(OBJECTIVE)]
    # Reference implementation WITHOUT the _isMeta skip → picks the carrier.
    def _neutered(messages):
        for i, m in enumerate(messages):
            if not isinstance(m, dict) or m.get('role') != 'user':
                continue
            if m.get('_isVuDirective') or m.get('_isVirtualUser'):
                continue
            c = m.get('content')
            if isinstance(c, str) and c.strip():
                return i
        return None
    assert _neutered(msgs) == 1           # neutered picks the carrier
    assert l2._objective_anchor_index(msgs) == 2  # real impl skips it


# ══════════════════════════════════════════════════════════
#  #3 — objective survives N successive compactions (VERBATIM)
# ══════════════════════════════════════════════════════════

def test_objective_survives_three_compactions(fake_summary):
    """★ CENTERPIECE — force-compact three times in a row; the objective must
    remain present VERBATIM and EXACTLY ONCE after each pass."""
    msgs = _build_long_conversation(30)
    assert _objective_present_verbatim(msgs)

    for pass_i in range(3):
        # tiny budget → boundary near the end → objective is in old_messages
        l2.execute_compact_tool(
                                msgs, task={'convId': 'c', 'id': 't', '_userId': 1},
                                preserve_budget_tokens=200)
        assert _objective_present_verbatim(msgs), \
            f'objective lost after compaction pass {pass_i + 1}'
        # Hole B — present EXACTLY once (no unbounded prefix duplication).
        count = sum(1 for m in msgs
                    if m.get('role') == 'user' and m.get('content') == OBJECTIVE)
        assert count == 1, f'objective duplicated ({count}×) after pass {pass_i + 1}'
        # add more filler so the next pass has something to compact again
        for i in range(20):
            msgs.append(_assistant(f'more work {pass_i}-{i} {"y" * 200}'))
            msgs.append(_user(f'next step {pass_i}-{i}'))


def test_anchor_position_is_right_after_system(fake_summary):
    """The re-inserted anchor sits immediately after the system block."""
    msgs = _build_long_conversation(30)
    l2.execute_compact_tool(
                            msgs, task={'convId': 'c', 'id': 't', '_userId': 1},
                            preserve_budget_tokens=200)
    # first non-system message must be the objective
    first_non_sys = next(m for m in msgs if m.get('role') != 'system')
    assert first_non_sys.get('content') == OBJECTIVE


def test_NC_anchor_removed_objective_is_summarized_away(monkeypatch, fake_summary):
    """NEUTER #3: disable the anchor exclusion (index→None) → the objective
    falls into old_messages, gets paraphrased by the fake summary, and is GONE
    from the live context. Proves the anchor is load-bearing."""
    monkeypatch.setattr(l2, '_objective_anchor_index', lambda messages: None)
    msgs = _build_long_conversation(30)
    assert _objective_present_verbatim(msgs)
    l2.execute_compact_tool(
                            msgs, task={'convId': 'c', 'id': 't', '_userId': 1},
                            preserve_budget_tokens=200)
    assert not _objective_present_verbatim(msgs), \
        'with the anchor neutered the objective must be summarized away'


# ══════════════════════════════════════════════════════════
#  #3 — head-truncate never drops the anchor
# ══════════════════════════════════════════════════════════

def test_head_truncate_preserves_objective():
    """L3 head-truncate drops oldest messages by token target but must never
    discard the objective anchor (now sitting right after system)."""
    # Layout after a compaction: system, OBJECTIVE, then many big messages.
    msgs = [_sys(), _user(OBJECTIVE)]
    for i in range(40):
        msgs.append(_assistant('big ' + 'z' * 4000))
        msgs.append(_user(f'turn {i}'))

    task = {'convId': 'c', 'id': 't'}
    # Aggressive token target forces heavy dropping.
    dropped = reactive._head_truncate(msgs, task, reported_token_count=10_000_000)
    assert dropped > 0
    assert _objective_present_verbatim(msgs), \
        'head-truncate must never drop the objective anchor'


def test_NC_head_truncate_without_protection_drops_objective(monkeypatch):
    """NEUTER: force the anchor index to None so _drop_pos falls back to popping
    system_end (the objective) → it gets dropped. Proves the protection is
    load-bearing."""
    import lib.tasks_pkg.compaction._reactive._headtrunc as headtrunc
    monkeypatch.setattr(
        headtrunc, '_objective_anchor_index', lambda messages: None)
    msgs = [_sys(), _user(OBJECTIVE)]
    for i in range(40):
        msgs.append(_assistant('big ' + 'z' * 4000))
        msgs.append(_user(f'turn {i}'))
    reactive._head_truncate(msgs, {'convId': 'c', 'id': 't'},
                            reported_token_count=10_000_000)
    assert not _objective_present_verbatim(msgs), \
        'without protection the objective should be dropped by head-truncate'


# ══════════════════════════════════════════════════════════
#  #1 — autopilotSummaries retention
# ══════════════════════════════════════════════════════════

class _FakeStore:
    """Persistent in-memory settings store mirroring update_conversation_settings."""
    def __init__(self):
        self.rows = {}

    def ensure(self, conv_id, **kw):
        self.rows.setdefault(conv_id, {}).update(kw)

    def update(self, conv_id, mutate, *, user_id=1, db=None, notify=True):
        # ``notify`` accepted (and ignored) — production passes notify=False
        # for internal run-bookkeeping writes; a fake without the kwarg turns
        # the call into a swallowed TypeError and the mutate never runs.
        if conv_id not in self.rows:
            return None
        mutate(self.rows[conv_id])
        return self.rows[conv_id]


@pytest.fixture
def store(monkeypatch):
    import lib.conversations as conv_pkg
    s = _FakeStore()
    monkeypatch.setattr(conv_pkg, 'update_conversation_settings', s.update)
    return s


def test_autopilot_summaries_retention_caps_growth(store, monkeypatch):
    """★ Conclude many runs; the map is capped at N most-recent, and the
    current run is always retained."""
    monkeypatch.setenv('TOFU_AUTOPILOT_SUMMARY_RETENTION', '5')
    import lib.tasks_pkg.autopilot as ap
    store.ensure('c1')
    for i in range(12):
        ap._store_run_record('c1', f'run-{i:02d}', reason='task_done',
                             text=f'report {i}', user_id=1)
    summaries = store.rows['c1']['autopilotSummaries']
    assert len(summaries) == 5, f'expected cap 5, got {len(summaries)}'
    # The most-recent run must always be present.
    assert 'run-11' in summaries
    # The oldest must have been evicted.
    assert 'run-00' not in summaries


def test_autopilot_summaries_unlimited_when_zero(store, monkeypatch):
    monkeypatch.setenv('TOFU_AUTOPILOT_SUMMARY_RETENTION', '0')
    import lib.tasks_pkg.autopilot as ap
    store.ensure('c2')
    for i in range(12):
        ap._store_run_record('c2', f'run-{i:02d}', reason='task_done', user_id=1)
    assert len(store.rows['c2']['autopilotSummaries']) == 12  # unlimited


def test_NC_summaries_retention_disabled_grows_unbounded(store, monkeypatch):
    """NEUTER #1: force retention→0 (unlimited) on the module the fn imports →
    the map grows unbounded (proves the cap is load-bearing)."""
    monkeypatch.setenv('TOFU_AUTOPILOT_SUMMARY_RETENTION', '5')
    import lib.agent_verdict as _av
    import lib.tasks_pkg.autopilot as ap
    monkeypatch.setattr(_av, 'autopilot_summary_retention', lambda: 0)
    store.ensure('c3')
    for i in range(12):
        ap._store_run_record('c3', f'run-{i:02d}', reason='task_done', user_id=1)
    assert len(store.rows['c3']['autopilotSummaries']) == 12


def test_hole_a_objective_pin_survives_run_conclude(store):
    """★ Hole A — _clear_run_id must NOT wipe autopilotObjective (so the next
    run reuses the durable pin instead of a drift-prone re-scan)."""
    import lib.tasks_pkg.autopilot as ap
    store.ensure('c4', autopilotObjective=OBJECTIVE, autopilotRunId='r1',
                 autopilotTurnCount=7, autopilotVuHistory=['a', 'b'])
    ap._clear_run_id('c4', user_id=1)
    s = store.rows['c4']
    assert s.get('autopilotObjective') == OBJECTIVE, 'objective pin must persist'
    # run-scoped counters ARE cleared
    assert 'autopilotRunId' not in s
    assert 'autopilotTurnCount' not in s
    assert 'autopilotVuHistory' not in s


# ══════════════════════════════════════════════════════════
#  #2 — transcript_archive ring-buffer retention
# ══════════════════════════════════════════════════════════

class _FakeArchiveStore:
    """In-memory transcript_archive keeping (id, conv) rows; prune keeps newest N."""
    def __init__(self):
        self.rows = []  # list of (id, conv_id)
        self._next = 1

    def insert(self, conv_id):
        self.rows.append((self._next, conv_id))
        self._next += 1
        return self._next - 1

    def prune_archives(self, conv_id, keep):
        if not conv_id or keep <= 0:
            return 0
        conv_rows = sorted((r for r in self.rows if r[1] == conv_id),
                           key=lambda r: r[0], reverse=True)
        if len(conv_rows) <= keep:
            return 0
        keep_ids = {r[0] for r in conv_rows[:keep]}
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if r[1] != conv_id or r[0] in keep_ids]
        return before - len(self.rows)


def test_prune_archives_ring_buffer():
    st = _FakeArchiveStore()
    for _ in range(10):
        st.insert('c1')
    st.insert('other')  # different conv, untouched
    deleted = st.prune_archives('c1', 4)
    assert deleted == 6
    c1_rows = [r for r in st.rows if r[1] == 'c1']
    assert len(c1_rows) == 4
    # newest ids retained (7,8,9,10)
    assert {r[0] for r in c1_rows} == {7, 8, 9, 10}
    # other conv untouched
    assert any(r[1] == 'other' for r in st.rows)


def test_prune_archives_noop_when_under_cap():
    st = _FakeArchiveStore()
    for _ in range(3):
        st.insert('c1')
    assert st.prune_archives('c1', 10) == 0
    assert st.prune_archives('c1', 0) == 0  # unlimited


def test_archive_retention_env_fail_open(monkeypatch):
    from lib.tasks_pkg.compaction._constants import (
        _ARCHIVE_RETENTION_DEFAULT, archive_retention)
    monkeypatch.delenv('TOFU_COMPACTION_ARCHIVE_RETENTION', raising=False)
    assert archive_retention() == _ARCHIVE_RETENTION_DEFAULT == 50
    monkeypatch.setenv('TOFU_COMPACTION_ARCHIVE_RETENTION', '0')
    assert archive_retention() == 0  # unlimited
    monkeypatch.setenv('TOFU_COMPACTION_ARCHIVE_RETENTION', 'junk')
    assert archive_retention() == 50  # fail-open
    monkeypatch.setenv('TOFU_COMPACTION_ARCHIVE_RETENTION', '20')
    assert archive_retention() == 20


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
