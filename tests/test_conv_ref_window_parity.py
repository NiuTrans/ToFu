# Incident anchor: born in commit 96d24277 — tool-registry SSOT: close the last provides gap + ratchet it shut; pi...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""conv_ref's window semantics must match the row store's, exactly.

Charter decision (2026-07-26): ``lib/database/messages_rows.load_message_window``
is the ONE implementation of "give me a slice of a conversation's messages".
``get_conversation``'s ``limit``/``before`` currently window IN MEMORY, because
``rows_read_enabled()`` is False and the backfill is stale — see the charter for
why that layering is deliberate rather than debt.

The charter also promises the eventual cutover is a PURE SWAP: replace
``_select_message_window`` with ``load_message_window``, touching no caller and
no test expectation. That promise is only true if the two agree on semantics
TODAY, while they are independent. A silent divergence (off-by-one on the
cursor, a different head/tail split, a different empty-case) would turn the
"pure swap" into a behaviour change discovered only after the migration flag
flips — exactly when it is most expensive to debug.

So this suite pins the CONTRACT both sides must satisfy:
  * ``limit`` = size of the tail window
  * ``before`` = EXCLUSIVE upper bound (1-based for the tool, 0-based/seq for
    the store) — the window ends on the message immediately before it
  * a window at/over the total returns everything, with no omission marker
  * paging backwards eventually reaches message #1 and terminates

It deliberately does NOT assert that conv_ref calls the row store — it must
keep passing before AND after the cutover. That is what makes it a migration
guard rather than a snapshot of today's wiring.
"""

import pytest

pytestmark = pytest.mark.unit


def _mk(n):
    return [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'M{i:04d}'} for i in range(n)]


def _mem_window(messages, head, tail, before=None):
    from lib.conv_ref._detail import _select_message_window
    kept, omitted, total = _select_message_window(
        messages, head, tail, before=before)
    return [i for i, _ in kept], omitted, total


class TestCursorSemantics:
    """`before` is an EXCLUSIVE upper bound on both sides."""

    def test_before_excludes_its_own_index(self):
        idx, _om, _tot = _mem_window(_mk(100), head=0, tail=5, before=50)
        assert idx == [45, 46, 47, 48, 49], (
            'before=50 must END at index 49 — an inclusive bound here would '
            'silently double-show one message per page during a page-up walk')

    def test_before_none_means_the_tail(self):
        idx, _om, _tot = _mem_window(_mk(100), head=0, tail=3)
        assert idx == [97, 98, 99]

    def test_before_zero_yields_nothing(self):
        idx, om, tot = _mem_window(_mk(100), head=0, tail=5, before=0)
        assert idx == [] and om == 0 and tot == 100

    def test_before_beyond_total_is_clamped(self):
        idx, _om, _tot = _mem_window(_mk(10), head=0, tail=3, before=9999)
        assert idx == [7, 8, 9]

    def test_negative_before_is_clamped_not_wrapped(self):
        """A negative cursor must not slice from the end Python-style."""
        idx, _om, _tot = _mem_window(_mk(10), head=0, tail=3, before=-5)
        assert idx == []


class TestWindowSizing:
    def test_window_at_total_returns_all_without_omission(self):
        idx, om, tot = _mem_window(_mk(8), head=3, tail=5)
        assert idx == list(range(8)) and om == 0 and tot == 8

    def test_window_over_total_returns_all(self):
        idx, om, _tot = _mem_window(_mk(4), head=3, tail=60)
        assert idx == [0, 1, 2, 3] and om == 0

    def test_head_and_tail_both_present_when_split(self):
        idx, om, tot = _mem_window(_mk(100), head=3, tail=5)
        assert idx[:3] == [0, 1, 2], 'head block lost'
        assert idx[-5:] == [95, 96, 97, 98, 99], 'tail block lost'
        assert om == 92 and tot == 100
        assert om == tot - len(idx)

    def test_omitted_count_is_exact(self):
        """A wrong count misleads the reader about how much is missing."""
        for n, head, tail in [(50, 3, 10), (200, 5, 20), (17, 2, 4)]:
            idx, om, tot = _mem_window(_mk(n), head=head, tail=tail)
            assert om == tot - len(idx), f'n={n} head={head} tail={tail}'

    def test_indices_are_ascending_and_unique(self):
        idx, _om, _tot = _mem_window(_mk(100), head=3, tail=5)
        assert idx == sorted(idx)
        assert len(idx) == len(set(idx)), 'head and tail blocks overlap'


class TestPagingTerminates:
    def test_walking_backwards_reaches_the_start(self):
        """A page-up walk must terminate, not stall or loop."""
        msgs = _mk(100)
        cursor, seen, steps = 100, set(), 0
        while cursor > 0 and steps < 50:
            idx, _om, _tot = _mem_window(msgs, head=0, tail=10, before=cursor)
            if not idx:
                break
            seen.update(idx)
            cursor = idx[0]
            steps += 1
        assert 0 in seen, 'page-up never reached the first message'
        assert steps < 50, 'page-up did not terminate'

    def test_consecutive_pages_do_not_overlap_or_gap(self):
        msgs = _mk(100)
        p1, _om, _tot = _mem_window(msgs, head=0, tail=10, before=100)
        p2, _om2, _tot2 = _mem_window(msgs, head=0, tail=10, before=p1[0])
        assert p2[-1] == p1[0] - 1, (
            f'page boundary broken: page1 starts {p1[0]}, page2 ends {p2[-1]}')
        assert not set(p1) & set(p2)


class TestRowStoreParity:
    """The row store must agree with the in-memory window on the SAME data.

    Runs against a fake DB shaped like the row store's SELECTs, so it exercises
    load_message_window's real slicing logic without depending on the stale
    backfill or on rows_read_enabled().
    """

    class _FakeDB:
        def __init__(self, n):
            self.n = n

        def execute(self, sql, params=()):
            outer = self

            class _Cur:
                def fetchone(self):
                    return {'n': outer.n}

                def fetchall(self):
                    s = sql.lower()
                    if 'seq<' in s.replace(' ', ''):
                        before, limit = int(params[1]), int(params[2])
                        rows = [{'meta': '{}', 'seq': i}
                                for i in range(before)][-limit:]
                        return list(reversed(rows))
                    if 'limit' in s:
                        limit = int(params[-1])
                        rows = [{'meta': '{}', 'seq': i}
                                for i in range(outer.n)][-limit:]
                        return list(reversed(rows))
                    return [{'meta': '{}', 'seq': i} for i in range(outer.n)]
            return _Cur()

    def test_tail_window_matches_memory(self):
        from lib.database.messages_rows import load_message_window
        w = load_message_window(self._FakeDB(100), 'c', limit=10)
        seqs = [w['firstLoadedSeq'], w['lastLoadedSeq']]
        mem, _om, _tot = _mem_window(_mk(100), head=0, tail=10)
        assert seqs == [mem[0], mem[-1]], (
            f'row store tail {seqs} != in-memory tail [{mem[0]}, {mem[-1]}]')
        assert w['totalCount'] == 100

    def test_page_up_matches_memory(self):
        from lib.database.messages_rows import load_message_window
        w = load_message_window(self._FakeDB(100), 'c', limit=10, before_seq=50)
        mem, _om, _tot = _mem_window(_mk(100), head=0, tail=10, before=50)
        assert [w['firstLoadedSeq'], w['lastLoadedSeq']] == [mem[0], mem[-1]], (
            'row store and in-memory disagree on the page-up window — the '
            'charter-promised "pure swap" cutover would change behaviour')

    def test_has_more_matches_omission(self):
        """hasMore and a non-zero omitted count must mean the same thing."""
        from lib.database.messages_rows import load_message_window
        w = load_message_window(self._FakeDB(100), 'c', limit=10)
        _idx, om, _tot = _mem_window(_mk(100), head=0, tail=10)
        assert w['hasMore'] is (om > 0)

    def test_full_window_agrees_on_no_more(self):
        from lib.database.messages_rows import load_message_window
        w = load_message_window(self._FakeDB(5), 'c', limit=10)
        _idx, om, _tot = _mem_window(_mk(5), head=0, tail=10)
        assert w['hasMore'] is False and om == 0


class TestCharterAlignment:
    def test_row_store_helper_still_exists_with_the_pinned_signature(self):
        """The charter names this function — a rename must fail loudly here."""
        import inspect
        from lib.database.messages_rows import load_message_window
        p = inspect.signature(load_message_window).parameters
        assert 'limit' in p and 'before_seq' in p

    def test_conv_ref_exposes_the_matching_knobs(self):
        import inspect
        from lib.conv_ref._detail import get_conversation
        p = inspect.signature(get_conversation).parameters
        assert 'limit' in p and 'before' in p


class TestHeadTailSelection:
    """The one-query row selector is byte-for-byte parity with memory."""

    class _FakeDB:
        def __init__(self, messages):
            import json
            self.rows = [
                {'meta': json.dumps(message), 'seq': seq}
                for seq, message in enumerate(messages)
            ]

        def execute(self, sql, params=()):
            low = sql.lower()
            all_rows = self.rows

            class _Cur:
                def fetchall(self_inner):
                    if 'union all' in low:
                        head = int(params[1])
                        start, end = int(params[3]), int(params[4])
                        return ([r for r in all_rows if r['seq'] < head]
                                + [r for r in all_rows
                                   if start <= r['seq'] < end])
                    end = int(params[1])
                    return [r for r in all_rows if r['seq'] < end]

            return _Cur()

    @pytest.mark.parametrize('n,head,tail,before', [
        (100, 3, 5, None),
        (8, 3, 5, None),
        (100, 3, 10, 50),
        (5, 3, 10, 0),
        (10, 0, 3, 999),
    ])
    def test_matches_memory(self, n, head, tail, before):
        from lib.database.messages_rows import load_message_selection

        messages = _mk(n)
        selected = load_message_selection(
            self._FakeDB(messages), 'c', head, tail,
            before_seq=before, known_total=n)
        memory, omitted, total = _mem_window(
            messages, head, tail, before=before)
        assert [seq for seq, _ in selected['kept']] == memory
        assert [message for _, message in selected['kept']] == [
            messages[seq] for seq in memory]
        assert selected['omitted'] == omitted
        assert selected['totalCount'] == total
