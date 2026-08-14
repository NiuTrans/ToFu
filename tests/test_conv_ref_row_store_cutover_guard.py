# Incident anchor: born in commit 28b5e520 — conv_ref: guard the row-store cutover — "pure swap" is data loss on a...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""The row-store cutover must fail OPEN on a not-yet-backfilled conversation.

Charter (2026-07-26) names ``load_message_window`` the one paging
implementation and promises the eventual cutover is a PURE SWAP of
conv_ref's in-memory ``_select_message_window`` for it.

That promise has a sharp edge the parity suite cannot see, because parity
compares the two windowers on the SAME data. Measured on the live DB:

    conversation_messages: 3,696 of 4,160 convs backfilled (88.8%)

For the other 464 the row store answers ``totalCount=0`` — indistinguishable
from "this conversation is empty". Real examples at the time of writing:

    mrlmtudriuexuo  msg_count=6  → row window totalCount=0
    mrnao32x86gnlw  msg_count=6  → row window totalCount=0
    mrk2dmaeybj5vd  msg_count=4  → row window totalCount=0

So a naive swap is not a pure swap on those rows — it is silent data loss,
and it looks like a correct empty result. ``routes/conversations.py`` already
learned this lesson (its blob tail-slice is the migration-flag-INDEPENDENT
safe default precisely "for the not-yet-backfilled convs where the row store
would serve an empty/short window"); conv_ref must inherit the same posture
rather than rediscover it after the flag flips.

:func:`row_window_usable` is that guard. It is wired into ``get_conversation``
NOW — while ``rows_read_enabled()`` is False and it is therefore inert — so the
protection exists BEFORE the migration rather than being remembered during it.
"""

import pytest

pytestmark = pytest.mark.unit


class _FakeDB:
    """Row-store shaped DB whose backfill state is controllable."""

    def __init__(self, n_rows, *, rev=3, mirror_rev=3, msg_count=None,
                 light_rows=None):
        self.n_rows = n_rows
        self.light_rows = n_rows if light_rows is None else light_rows
        self.rev = rev
        self.mirror_rev = mirror_rev
        self.msg_count = n_rows if msg_count is None else msg_count

    def execute(self, sql, params=()):
        outer = self

        class _Cur:
            def fetchone(self):
                return {'n': outer.n_rows, 'light_n': outer.light_rows}

            def fetchall(self):
                if 'from conversations' in sql.lower():
                    return [{
                        'rev': outer.rev,
                        'messages_rows_rev': outer.mirror_rev,
                        'msg_count': outer.msg_count,
                    }]
                rows = [{'meta': '{}', 'seq': i} for i in range(outer.n_rows)]
                s = sql.lower().replace(' ', '')
                if 'seq<' in s:
                    before, limit = int(params[1]), int(params[2])
                    return list(reversed(rows[:before][-limit:]))
                if 'limit' in s:
                    return list(reversed(rows[-int(params[-1]):]))
                return rows
        return _Cur()


class TestGuardExists:
    def test_helper_is_exported(self):
        from lib.conv_ref._detail import row_window_usable
        assert callable(row_window_usable)

    def test_get_conversation_consults_it(self):
        """The guard must be wired, not merely defined.

        A helper nobody calls protects nothing — this is the same shape as the
        identity-gate tripwire lesson (a predicate that is never consulted
        fails open silently).
        """
        import inspect
        from lib.conv_ref import _detail
        src = inspect.getsource(_detail.get_conversation)
        assert 'row_window_usable' in src


class TestFailsOpenWhenNotBackfilled:
    def test_empty_row_store_is_not_usable_for_a_nonempty_conv(self):
        """totalCount=0 vs a conv that HAS messages → refuse the row store."""
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(_FakeDB(0), 'c1', blob_count=6) is False

    def test_short_row_store_is_not_usable(self):
        """A PARTIAL backfill is the nastier case — it looks plausible.

        An empty window at least reads as suspicious; a window that is merely
        SHORT silently drops the oldest history with no signal at all.
        """
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(_FakeDB(3), 'c1', blob_count=20) is False

    def test_complete_row_store_is_usable(self):
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(
            _FakeDB(20, msg_count=20), 'c1', blob_count=20) is True

    def test_missing_light_projection_fails_closed(self):
        """A byte-complete mirror is not yet fast-read-complete after v47."""
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(
            _FakeDB(20, msg_count=20, light_rows=19),
            'c1', blob_count=20) is False

    def test_row_store_ahead_of_blob_is_rejected_as_stale_tail(self):
        """Extra rows can be a failed truncation mirror; never serve them."""
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(_FakeDB(21), 'c1', blob_count=20) is False

    def test_genuinely_empty_conversation_is_fine(self):
        """0 rows for a 0-message conv is agreement, not a missing backfill."""
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(
            _FakeDB(0, msg_count=0), 'c1', blob_count=0) is True

    def test_revision_zero_marker_is_not_treated_as_missing(self):
        """Freshly-created rev=0 rows are a valid, current mirror."""
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(
            _FakeDB(0, rev=0, mirror_rev=0, msg_count=0),
            'c1', blob_count=0) is True

    def test_equal_count_but_stale_revision_is_rejected(self):
        """The old count-only gate accepted this silent-corruption shape."""
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(
            _FakeDB(20, rev=4, mirror_rev=3, msg_count=20),
            'c1', blob_count=20) is False

    def test_db_error_fails_open(self):
        """Any error must choose the authoritative blob, never the row store."""
        class _Boom:
            def execute(self, *a, **k):
                raise RuntimeError('db down')
        from lib.conv_ref._detail import row_window_usable
        assert row_window_usable(_Boom(), 'c1', blob_count=6) is False


class TestInertWhileFlagIsOff:
    def test_reads_still_come_from_the_blob_today(self):
        """With the flag off, behaviour must be byte-identical to before.

        The guard is landed EARLY on purpose; it must not change any output
        until the migration flag flips.
        """
        import json

        from lib.conv_ref import _detail

        msgs = [{'role': 'user', 'content': f'M{i}'} for i in range(6)]
        row = {
            'id': 'c1', 'user_id': 1, 'title': 'T',
            'messages': json.dumps(msgs), 'created_at': 1, 'updated_at': 2,
            'settings': '{}', 'msg_count': 6, 'rev': 1,
        }

        class _Cur:
            def fetchone(self):
                return row

        class _DB:
            def execute(self, *a, **k):
                return _Cur()

        orig = _detail._get_db
        _detail._get_db = lambda: _DB()
        try:
            out = _detail.get_conversation('c1')
        finally:
            _detail._get_db = orig
        for i in range(6):
            assert f'M{i}' in out, 'blob path stopped returning all messages'


class TestPartialBackfillEndToEnd:
    """The case that actually loses data, driven through get_conversation.

    An EMPTY row store is caught twice over (the coverage guard, and the
    ``if w['messages'] and first is not None`` emptiness check inside the
    branch), so removing the guard does NOT regress it — a neutering aimed
    at the empty case therefore proves nothing. I ran exactly that neutering
    first and it did not bite; this class is the result of chasing that down.

    A PARTIAL backfill is the case with exactly one line of defence. Measured
    with the guard stripped: a 20-message conversation whose row store holds
    only the first 5 renders messages 0-4 and silently drops 5-19 INCLUDING
    THE CONCLUSION — the precise failure d48f74ce fixed at the rendering
    layer, reintroduced from underneath at the storage layer.
    """

    @staticmethod
    def _partial_db(total, backfilled):
        import json
        msgs = [{'role': 'user' if i % 2 == 0 else 'assistant',
                 'content': f'MSG{i:04d}'} for i in range(total)]
        row = {
            'id': 'p1', 'user_id': 1, 'title': 'T',
            'messages': json.dumps(msgs), 'created_at': 1, 'updated_at': 2,
            'settings': '{}', 'msg_count': total, 'rev': 1,
            'messages_rows_rev': 1,
        }

        class _Cur:
            def __init__(self, v):
                self.v = v

            def fetchone(self):
                return self.v

            def fetchall(self):
                return self.v if isinstance(self.v, list) else [self.v]

        class _DB:
            def execute(self, sql, params=()):
                low = sql.lower()
                if 'count(*)' in low:
                    return _Cur({'n': backfilled, 'light_n': backfilled})
                if ('from conversations' in low
                        and 'messages_rows_rev' in low):
                    return _Cur({
                        'rev': row['rev'],
                        'messages_rows_rev': row['messages_rows_rev'],
                        'msg_count': row['msg_count'],
                    })
                if 'from conversation_messages' in low:
                    all_rows = [{'meta': json.dumps(msgs[i]), 'seq': i}
                                for i in range(backfilled)]
                    if 'union all' in low:
                        head = int(params[1])
                        start, end = int(params[3]), int(params[4])
                        selected = ([r for r in all_rows if r['seq'] < head]
                                    + [r for r in all_rows
                                       if start <= r['seq'] < end])
                        return _Cur(selected)
                    if 'seq<' in low.replace(' ', '') and 'limit' not in low:
                        end = int(params[1])
                        return _Cur([r for r in all_rows if r['seq'] < end])
                    return _Cur(list(reversed(
                        all_rows[-int(params[-1]):])))
                return _Cur(row)
        return _DB()

    def test_partial_backfill_still_renders_every_message(self, monkeypatch):
        from lib.conv_ref import _detail
        monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
        monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
        monkeypatch.setattr(_detail, '_get_db',
                            lambda: self._partial_db(20, 5))
        out = _detail.get_conversation('p1')
        missing = [i for i in range(20) if f'MSG{i:04d}' not in out]
        assert not missing, (
            f'row store had 5 of 20 rows and the guard let it serve — '
            f'messages {missing} were silently dropped')

    def test_partial_backfill_keeps_the_conclusion(self, monkeypatch):
        """The tail is what a reader opens a past conversation FOR."""
        from lib.conv_ref import _detail
        monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
        monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
        monkeypatch.setattr(_detail, '_get_db',
                            lambda: self._partial_db(20, 5))
        assert 'MSG0019' in _detail.get_conversation('p1')

    def test_complete_backfill_is_allowed_to_serve(self, monkeypatch):
        """The guard must not be a permanent veto — a full row store passes."""
        from lib.conv_ref import _detail
        monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
        monkeypatch.setenv('TOFU_MESSAGES_ROWS_READ', '1')
        monkeypatch.setattr(_detail, '_get_db',
                            lambda: self._partial_db(20, 20))
        out = _detail.get_conversation('p1')
        assert 'MSG0019' in out and 'MSG0000' in out


class TestLiveBackfillIsIncomplete:
    """Pin the measured reality the guard exists for.

    If a later backfill closes the gap this test still passes (it only asserts
    the guard is correct for whatever the coverage is) — but it documents, in
    executable form, that partial coverage is the state that motivated it.
    """

    def test_partial_coverage_is_the_dangerous_case(self):
        from lib.conv_ref._detail import row_window_usable
        # 3,696 of 4,160 backfilled → the 464 remainder look empty.
        assert row_window_usable(_FakeDB(0), 'not-backfilled', blob_count=6) is False
        assert row_window_usable(
            _FakeDB(6, msg_count=6), 'backfilled', blob_count=6) is True
