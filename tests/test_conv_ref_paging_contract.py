"""``get_conversation`` must not promise a parameter it does not accept.

Two defects this pins, both measured on production rows before the fix:

1. **The description advertised paging that could not happen.** The tool text
   said "`before` pages backwards through the rest" and the result footer said
   "re-read with before=N", but the schema exposed only
   ``conversation_id`` / ``include_tool_details`` / ``raw`` and
   ``execute_conv_ref_tool`` never forwarded ``limit`` / ``before`` to the
   library. A model following that instruction had its argument silently
   dropped, got the IDENTICAL window back, and had no way to detect the loop —
   a precisely-worded, credible, WRONG signal.

2. **A "raw" read delivered 0.4-1.0% of the record and looked complete.** The
   window was sized by ``TRANSCRIPT_TAIL`` (60), a constant tuned for the prose
   renderer that truncates tool results as it goes. A raw record carries every
   message's full ``toolRounds``, so asking for 60 blew the budget and the
   over-limit guards demolished the window after the fact: measured 2 of 205
   messages, field-clamped, under a header that read "Messages: 2 of 205" in
   the same breath as a promise that nothing was summarized away.

The guards below assert RESULTS, never constants: the budget arithmetic,
``MAX_CHARS`` and the fitted window sizes may all be retuned without turning
these red, but a dropped paging argument or a re-broken promise fails
immediately.

The ``test_every_parameter_named_in_the_description_exists`` case is the
general one — it derives the claim from the description text itself, so it
also catches the NEXT parameter someone promises in prose without wiring.
"""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _big_conversation(n=220, body_chars=3000):
    """A conversation too large for one window, shaped like a real one.

    Each assistant message carries a fat ``toolRounds`` payload — that is what
    makes a raw record heavy, and sizing off it is the whole point.

    The sizes are DELIBERATELY uneven, mirroring measured production rows
    (median ~9 KB, individual messages 80-100 KB, one opener 46 KB). A uniform
    fixture is too kind: with every message the same small size, ``60`` of them
    happen to fit the budget, so reverting to the prose-tuned constant would
    NOT be caught — verified, the neuter did not bite until the shape matched
    reality.

    The payload is also MANY SMALL NESTED KEYS rather than one giant string.
    That is what makes pretty-printing (``indent=2``) inflate a real record by
    ~13% (measured on production): indentation costs per line, and a deep
    dict-of-lists has thousands of them. A fixture built from one long string
    inflates by almost nothing, so it cannot detect a size probe that measures
    compact JSON while the dump is pretty-printed.
    """
    msgs = []
    for i in range(n):
        if i % 2 == 0:
            msgs.append({'role': 'user', 'content': f'MSG{i:04d} question',
                         '_msgId': f'u{i}', 'timestamp': 1700000000000 + i})
        else:
            # Every 7th assistant round is a heavyweight, like a real tool dump.
            weight = 12 if i % 7 == 0 else 1
            rounds = []
            for r in range(3 * weight):
                rounds.append({
                    'toolName': 'read_files', 'status': 'done',
                    'args': {'path': f'lib/f{i}_{r}.py', 'start_line': r,
                             'end_line': r + 40, 'encoding': 'utf-8'},
                    'results': [{'text': 'R' * (body_chars // 8),
                                 'line': r, 'file': f'lib/f{i}_{r}.py',
                                 'ok': True}],
                })
            msgs.append({
                'role': 'assistant', 'content': f'MSG{i:04d} answer',
                '_msgId': f'a{i}', 'model': 'test-model-x',
                'finishReason': 'stop',
                'usage': {'input_tokens': 10 + i, 'output_tokens': 3},
                'toolRounds': rounds,
            })
    return msgs


class _Row(dict):
    """dict that also answers ``row['col']`` like the DB wrapper."""


@pytest.fixture
def big_conv(monkeypatch):
    from lib.conv_ref import _detail
    msgs = _big_conversation()
    row = _Row({
        'id': 'big1', 'user_id': 1, 'title': 'Big One',
        'messages': msgs, 'created_at': 1, 'updated_at': 2,
        'settings': {}, 'msg_count': len(msgs), 'rev': 7,
    })
    monkeypatch.setattr(
        _detail, '_read_conversation_snapshot',
        lambda conversation_id, *, user_id: row)
    return msgs


def _run(**kw):
    from lib.conv_ref import execute_conv_ref_tool
    kw.setdefault('conversation_id', 'big1')
    return execute_conv_ref_tool('get_conversation', kw, user_id=1)


def _record(out):
    return json.loads(out.split('```json', 1)[1].rsplit('```', 1)[0])


def _cursor(out):
    """The paging cursor the PRODUCT tells the caller to use.

    Taken from the emitted header rather than recomputed here: a test that
    derives its own cursor would be testing its own copy of the rule, and
    would stay green even if the header advertised an unusable one (it once
    advertised ``before=1``, which returns nothing).
    """
    m = re.search(r'before=(\d+)', out.split('```json', 1)[0])
    return int(m.group(1)) if m else None


class TestPagingArgumentsAreReallyWired:
    """The decisive behaviour: a paging arg must CHANGE the window."""

    def test_before_moves_the_window_backwards(self, big_conv):
        out = _run()
        first = _record(out)
        cursor = _cursor(out)
        assert cursor, 'a windowed read advertised no paging cursor'
        second = _record(_run(before=cursor))
        assert max(second['messageIndices']) < max(first['messageIndices']), (
            'before= did not move the window — the paging argument is being '
            'dropped, so the model loops on the same window forever')
        assert max(second['messageIndices']) < cursor, (
            'before is documented as EXCLUSIVE; it returned its own index')

    def test_paging_reaches_messages_the_default_omitted(self, big_conv):
        """The point of paging: get at content one call cannot carry.

        Follows the cursor the header ADVERTISES at each step — the exact loop
        a model runs. An advertised cursor that cannot advance is the failure
        this catches.
        """
        out = _run()
        first = _record(out)
        seen = set(first['messageIndices'])
        cursor = _cursor(out)
        for _ in range(6):
            if not cursor:
                break
            page_out = _run(before=cursor)
            seen |= set(_record(page_out)['messageIndices'])
            nxt = _cursor(page_out)
            if nxt is None or nxt >= cursor:
                break
            cursor = nxt
        assert len(seen) > len(first['messageIndices']), (
            'walking the advertised cursor never yielded a message the first '
            'window lacked — paging is decorative')

    def test_limit_changes_the_window_size(self, big_conv):
        wide = _record(_run())
        narrow = _record(_run(limit=1))
        assert len(narrow['messages']) < len(wide['messages'])

    def test_stringified_paging_args_are_honoured(self, big_conv):
        """Models emit JSON numbers as strings; dropping one is a silent lie."""
        typed = _record(_run(before=40))
        stringy = _record(_run(before='40'))
        assert typed['messageIndices'] == stringy['messageIndices']

    @pytest.mark.parametrize('bad', ['abc', 0, -5])
    def test_unusable_paging_args_report_instead_of_being_ignored(
            self, big_conv, bad):
        """Silently ignoring a bad cursor recreates the original defect: the
        caller asks to page, gets the same window, and cannot tell why."""
        out = _run(before=bad)
        assert out.lstrip().startswith('Error:'), (
            f'before={bad!r} was silently ignored instead of reported')


class TestTheHeaderStatesWhatWasDelivered:
    def test_delivery_is_stated_up_front(self, big_conv):
        out = _run()
        head = out.split('```json', 1)[0]
        rec = _record(out)
        assert 'DELIVERED' in head
        m = re.search(r'DELIVERED\s+(\d+)\s+of\s+(\d+)', head)
        assert m, f'no delivery line in header: {head!r}'
        assert int(m.group(1)) == len(rec['messages']), (
            'the header count disagrees with the payload it introduces')
        assert int(m.group(2)) == rec['messageCount']

    def test_a_partial_read_says_how_to_continue(self, big_conv):
        head = _run().split('```json', 1)[0]
        assert 'before' in head and 'limit' in head, (
            'a windowed read gave no way to reach the rest — a dead end')

    def test_clamping_is_disclosed_in_the_header(self, monkeypatch):
        """A field-clamped read must not look like a faithful one.

        Uses one message far larger than the whole budget, which is the real
        shape that forces clamping (measured max on production: 436 KB).
        """
        from lib.conv_ref import _detail
        row = _Row({
            'id': 'big1', 'user_id': 1, 'title': 'Huge',
            'messages': [
                {'role': 'user', 'content': 'q' * 400000, '_msgId': 'u1'}],
            'created_at': 1, 'updated_at': 2, 'settings': {},
            'msg_count': 1, 'rev': 1,
        })
        monkeypatch.setattr(
            _detail, '_read_conversation_snapshot',
            lambda conversation_id, *, user_id: row)
        out = _run()
        rec = _record(out)
        head = out.split('```json', 1)[0]
        if rec.get('fieldsClamped') or rec.get('reducedToFinalMessage'):
            assert 'CLAMPED' in head or 'reduced' in head, (
                'fields were clamped but the header presented the record as '
                'delivered intact')


class TestTheDefaultWindowIsNotDemolished:
    def test_default_read_keeps_whole_messages_when_it_can(self, big_conv):
        """Sizing the window off real message weight, instead of a constant
        tuned for the prose renderer, is what stops the over-budget guards
        from demolishing a raw read down to a clamped remnant."""
        rec = _record(_run())
        assert not rec.get('fieldsClamped'), (
            'the default raw read still had to clamp per-message fields — the '
            'window is being sized beyond what the budget can deliver whole')
        assert len(rec['messages']) >= 3

    def test_the_recent_window_has_no_holes_punched_in_it(self, big_conv):
        """The decisive property, and the one that catches an oversized ask.

        Requesting more messages than the budget can hold does NOT fail
        cleanly — the over-budget guard evicts messages from the MIDDLE of the
        window, leaving a record that reads as consecutive history but has
        silent gaps (measured with the prose constant: ...161, 217, 218...,
        a 55-message hole with nothing marking it). Sizing the ask up front
        means the recent block is contiguous, so what the reader sees really
        is what happened next.
        """
        rec = _record(_run())
        idx = rec['messageIndices']
        # The head block is the leading run of consecutive indices starting at
        # #1; everything after it is the recent window. Deriving the split this
        # way (rather than guessing an offset) matters — a naive slice can step
        # straight over an evicted gap and report contiguity that isn't there.
        cut = 1
        while cut < len(idx) and idx[cut] == idx[cut - 1] + 1:
            cut += 1
        tail = idx[cut:]
        holes = [(a, b) for a, b in zip(tail, tail[1:]) if b - a > 1]
        assert not holes, (
            f'the recent window has gaps at {holes} — messages were evicted '
            f'from the middle after the fact, so consecutive-looking history '
            f'is actually missing turns. indices={idx}')

    def test_the_window_uses_the_budget_it_has(self, big_conv):
        """A read that carries far fewer messages than fit is wasting the
        budget — the failure mode when the window is sized by a constant and
        then demolished, rather than fitted."""
        from lib.conv_ref._detail import MAX_CHARS
        out = _run()
        rec = _record(out)
        assert len(out) > MAX_CHARS * 0.5, (
            f'only {len(out):,} of a {MAX_CHARS:,} char budget was used while '
            f'{rec["messageCount"] - len(rec["messages"])} messages went '
            f'undelivered')

    def test_the_fitted_window_actually_fits_when_serialized(self, big_conv):
        """The fitted ask must land close to the budget, not wildly over it.

        The size probe can only ESTIMATE: pretty-printing costs indentation
        per line, so the true cost depends on nesting depth. That residual
        drift is fine — the overflow guard trims the oldest message and the
        header reports it. What must not happen is a probe so wrong that the
        guard has to demolish the window, which is how a 60-message ask
        collapsed to 2 clamped messages.

        Asserting "no clamping needed" rather than "the estimate was exact"
        keeps this honest: retuning the budget or the serializer is free, but
        a probe that badly under-counts fails immediately.
        """
        rec = _record(_run())
        assert not rec.get('fieldsClamped'), (
            'the fitted window overflowed all the way into the clamp path — '
            'the size probe is far enough off that the guard had to destroy '
            'the window instead of trimming it')
        assert len(rec['messages']) >= 3, (
            f'only {len(rec["messages"])} messages survived — the ask was '
            f'sized so far past the budget that eviction gutted it')

    def test_the_ending_survives(self, big_conv):
        rec = _record(_run())
        assert max(rec['messageIndices']) == rec['messageCount'], (
            'the most recent message is missing — a debugging read is usually '
            'about how the conversation ENDED')

    def test_opening_context_survives(self, big_conv):
        rec = _record(_run())
        assert min(rec['messageIndices']) == 1, (
            'the opening message is gone, so the record has no "what was this '
            'about" anchor')

    def test_output_stays_bounded_and_parseable(self, big_conv):
        from lib.conv_ref._detail import MAX_CHARS
        out = _run()
        assert len(out) <= MAX_CHARS * 1.1
        _record(out)  # must not raise


class TestDescriptionMatchesTheSchema:
    """The general guard: prose is not checked by anything else.

    Derived from the description TEXT rather than a hand-listed set, so it also
    catches the next parameter someone promises without wiring it.
    """

    def _fn(self):
        from lib.tools.conversation import CONV_REF_GET_TOOL
        return CONV_REF_GET_TOOL['function']

    def test_every_parameter_named_in_the_description_exists(self):
        fn = self._fn()
        props = set(fn['parameters']['properties'])
        # Backticked identifiers are how this description names arguments.
        named = set(re.findall(r'`([a-z_][a-z0-9_]*)`', fn['description']))
        # Field names of the RESULT record are not parameters.
        result_fields = {'messageCount', 'omitted', 'truncated'}
        promised = {n for n in named if n not in result_fields}
        missing = promised - props
        assert not missing, (
            f'the description promises parameter(s) {sorted(missing)} that the '
            f'schema does not expose — a model that follows the instruction '
            f'has its argument silently dropped. Exposed: {sorted(props)}')

    def test_the_executor_forwards_every_exposed_parameter(self, big_conv):
        """Schema presence is not enough — an exposed-but-ignored parameter is
        the same lie one layer down. Driven behaviourally per parameter."""
        base_out = _run()
        base = _record(base_out)
        assert _record(_run(limit=1))['messageIndices'] != base['messageIndices']
        cursor = _cursor(base_out)
        assert cursor, 'no cursor advertised'
        assert _record(_run(before=cursor))['messageIndices'] != base['messageIndices']
        assert 'Referenced Conversation' in _run(raw=False)
