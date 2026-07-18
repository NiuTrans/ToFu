"""Forward fix: on-open narration-segment backfill
(lib/translate/segment_backfill.py).

THE GAP
-------
A turn whose DELIVERABLE was translated (``translatedContent`` set,
``_translateDone``) but whose interleaved narration segments were never stamped
with ``translatedText`` shows Chinese only at the bottom and English narration
in the timeline — and NEVER recovers on its own (both ``needsTranslation`` and
the retro guard treat the turn as done, so nothing re-requests it). The one-shot
migration fixes existing rows; THIS is the forward fix invoked on conversation
OPEN.

WHAT THIS TESTS
---------------
``backfill_conv_narration_segments(conv_id)`` end-to-end against fakes for the
async DB + the LLM:
  • a candidate conversation (``translatedContent`` set, narration ``narrTr==0``)
    gets every narration segment stamped → after the call ``narrTr == narrSeg``;
  • the rev-neutral write is issued (messages UPDATE guarded on rev, then rev
    reset — updated_at untouched);
  • idempotent: a second pass finds all narration stamped → no write.

BITING NEGATIVE CONTROLS
------------------------
  • ``conv_has_backfill_candidates`` / ``needs_segment_narration_translation``
    return False for a turn whose narration is ALREADY fully stamped → the GET
    path would NOT spawn the task (no candidate) → narration stays as-is (proves
    the trigger is candidate-gated, not fired blindly).
  • With the enrich-only translate core neutered off (segments treated as
    already-done), the backfill stamps NOTHING → ``narrTr`` stays 0 (proves the
    translate-core invocation is the load-bearing step, not incidental).

Thinking segments are never stamped (backend contract) — asserted.
"""

import asyncio
import json

import lib.translate.runtime as rt
import lib.translate.segment_backfill as sb


def _fake_tf(text, system_prompt, source='', target='', **kw):
    return ('ZH:' + text), {'_dispatch': {'model': 'fake-mt'}}


def _msg(stamped=False):
    """An already-translated assistant turn: 2 narration segs + 1 thinking +
    2 tool_use + 1 deliverable. narration un-stamped unless ``stamped``."""
    seg0 = {'type': 'text', 'text': 'Let me read the files.',
            'deliverable': False, 'llmRound': 0}
    seg1 = {'type': 'text', 'text': 'Now let me check the tests.',
            'deliverable': False, 'llmRound': 1}
    if stamped:
        seg0['translatedText'] = 'ZH:Let me read the files.'
        seg1['translatedText'] = 'ZH:Now let me check the tests.'
    return {
        'role': 'assistant', 'content': 'The answer.',
        'translatedContent': 'ZH:The answer.', '_translateDone': True,
        '_msgId': 'm1',
        'segments': [
            {'type': 'thinking', 'text': 'reasoning', 'llmRound': 0},
            seg0,
            {'type': 'tool_use', 'id': 't0', 'llmRound': 0},
            seg1,
            {'type': 'tool_use', 'id': 't1', 'llmRound': 1},
            {'type': 'text', 'text': 'The answer.', 'deliverable': True, 'terminal': True},
        ],
    }


def _narr_counts(msg):
    """(narrSeg, narrTr) for a message — narration segments total vs stamped."""
    narr = [s for s in msg.get('segments', [])
            if isinstance(s, dict) and s.get('type') == 'text'
            and not s.get('deliverable') and s.get('llmRound') is not None
            and (s.get('text') or '').strip()]
    narr_tr = sum(1 for s in narr if (s.get('translatedText') or '').strip())
    return len(narr), narr_tr


# ── Candidate predicate (trigger gate) ───────────────────────────────────────

def test_predicate_true_for_translated_turn_with_unstamped_narration():
    assert sb.needs_segment_narration_translation(_msg()) is True


def test_predicate_false_when_narration_already_stamped():
    assert sb.needs_segment_narration_translation(_msg(stamped=True)) is False


def test_conv_has_candidates_gate():
    assert sb.conv_has_backfill_candidates([_msg()]) is True
    assert sb.conv_has_backfill_candidates([_msg(stamped=True)]) is False
    assert sb.conv_has_backfill_candidates([]) is False


# ── Async DB fakes ───────────────────────────────────────────────────────────

class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeTxnConn:
    def __init__(self, current_rev):
        self.current_rev = current_rev
        self.calls = []
        self.messages_written = None
        self.rev_after = current_rev

    async def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        self.calls.append((s, params))
        if s.startswith('UPDATE conversations SET messages='):
            _msgs, _cid, cas_rev = params
            if cas_rev != self.current_rev:
                return _Cursor(0)
            self.messages_written = _msgs
            self.rev_after = self.current_rev + 1
            return _Cursor(1)
        if s.startswith('UPDATE conversations SET rev='):
            new_rev, _cid = params
            self.rev_after = new_rev
            return _Cursor(1)
        return _Cursor(0)


class _FakeTxnCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _patch_db(monkeypatch, messages, rev=3):
    """Fake async_fetchone (row read) + async_transaction (rev-neutral write).

    backfill_conv_narration_segments imports these INSIDE the function from
    ``lib.database`` / ``lib.database.aio``, so patch them on those modules.
    """
    row = {'messages': json.dumps(messages), 'rev': rev}
    conn = _FakeTxnConn(current_rev=rev)

    import lib.database as dbmod
    import lib.database.aio as aiomod

    async def _fake_fetchone(sql, params=(), **kw):
        return row

    monkeypatch.setattr(dbmod, 'async_fetchone', _fake_fetchone, raising=False)
    monkeypatch.setattr(aiomod, 'async_transaction', lambda: _FakeTxnCtx(conn), raising=False)
    return conn


# ── End-to-end: on-open backfill stamps narration ───────────────────────────

def test_onopen_backfill_stamps_missing_narration(monkeypatch):
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)
    messages = [_msg()]
    n_seg, n_tr = _narr_counts(messages[0])
    assert (n_seg, n_tr) == (2, 0), 'fixture must start un-stamped'

    conn = _patch_db(monkeypatch, messages, rev=3)
    summary = asyncio.run(sb.backfill_conv_narration_segments('c1'))

    # The stamped messages were written back; re-parse what the fake persisted.
    assert conn.messages_written is not None, 'expected a rev-neutral write'
    written = json.loads(conn.messages_written)
    n_seg2, n_tr2 = _narr_counts(written[0])
    assert n_seg2 == 2 and n_tr2 == 2, f'narrTr must reach narrSeg: {(n_seg2, n_tr2)}'
    assert written[0]['segments'][1]['translatedText'] == 'ZH:Let me read the files.'
    assert written[0]['segments'][3]['translatedText'] == 'ZH:Now let me check the tests.'
    # Thinking segment NEVER stamped (backend contract).
    assert 'translatedText' not in written[0]['segments'][0]
    # Deliverable / tool_use never stamped.
    assert 'translatedText' not in written[0]['segments'][5]
    assert summary['segmentsStamped'] == 2 and summary['wrote'] is True
    # rev held (not bumped) → no spurious client CAS 409.
    assert conn.rev_after == 3


def test_onopen_backfill_rev_neutral_no_updated_at(monkeypatch):
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)
    conn = _patch_db(monkeypatch, [_msg()], rev=5)
    asyncio.run(sb.backfill_conv_narration_segments('c1'))
    # messages UPDATE then rev-reset; nothing touches updated_at.
    assert any(c[0].startswith('UPDATE conversations SET messages=') for c in conn.calls)
    assert any(c[0].startswith('UPDATE conversations SET rev=') for c in conn.calls)
    assert all('updated_at' not in c[0] for c in conn.calls)
    assert conn.rev_after == 5


def test_onopen_backfill_idempotent_second_pass(monkeypatch):
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)
    # Already fully stamped → no candidate → no write at all.
    conn = _patch_db(monkeypatch, [_msg(stamped=True)], rev=3)
    summary = asyncio.run(sb.backfill_conv_narration_segments('c1'))
    assert summary['wrote'] is False and summary['segmentsStamped'] == 0
    assert conn.messages_written is None, 'must not write when nothing to stamp'


# ── Biting negative control: neuter the translate core → narrTr stays 0 ──────

def test_nc_neutered_core_stamps_nothing(monkeypatch):
    """If the translate core is neutered to translate NOTHING (mimicking the
    absence of the backfill invocation), narration stays un-stamped → narrTr
    remains 0. Proves the ``_translate_segments_to_map`` call is load-bearing."""
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)

    def _empty_core(segs, system_prompt, source, target, *, log_tag='?'):
        return {}  # translate nothing (the pre-fix world: no backfill runs)

    monkeypatch.setattr(rt, '_translate_segments_to_map', _empty_core)
    conn = _patch_db(monkeypatch, [_msg()], rev=3)
    summary = asyncio.run(sb.backfill_conv_narration_segments('c1'))
    assert summary['segmentsStamped'] == 0 and summary['wrote'] is False
    assert conn.messages_written is None
    # The in-memory message narration is still 0/2 — the bug persists without
    # the (real) core.
    assert _narr_counts(_msg()) == (2, 0)
