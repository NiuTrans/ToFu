"""Segment-less narration backfill — the ROOT-CAUSE regression for
"historical tool-call narration never gets auto-translated".

DIAGNOSIS (evidence-locked against the live PostgreSQL row for conv
``mrx815iwc3zrtr`` msg[1], 2026-07-23)
------------------------------------------------------------------------
The reported turn persisted with a ``toolRounds`` array (21 rounds, each
carrying its English ``assistantContent`` narration) but **NO ``segments``
array at all** — while a clean-finalize sibling turn (msg[3]) DID carry
segments. The authoritative thin segments (153 KB) lived in
``task_results.segments`` keyed on the message's ``_taskId``, but the GET-path
backstop ``_rehydrate_segments_from_task_results`` is DISPLAY-ONLY (never writes
back), so the stored ``messages`` row stayed segment-less forever.

Every narration-translation path keys on ``msg.segments[*].translatedText``
(``has_untranslated_narration`` / ``needs_segment_narration_translation`` /
``_build_segment_translation_map`` → ``_read_message_segments``). All of them
short-circuit to "nothing to do" when ``segments`` is absent, so the turn was
INVISIBLE to translation — the deliverable was already Chinese (skip) and the
interleaved English narration in ``toolRounds`` was never touched.

The extra insult: the Chinese for most rounds was ALREADY computed live and
persisted on the message as ``_translatePartialByRound`` ({str(round): 中文}),
but no settle/backfill path ever read it back — it sat unused.

THE FIX makes ``backfill_message_narration_sync`` SEGMENT-INDEPENDENT: when the
message has no usable ``segments`` but carries ``toolRounds``, it synthesises the
narration map from ``toolRounds[*].assistantContent``, REUSING any Chinese
already present in ``_translatePartialByRound`` (zero LLM) and translating only
the rounds that field doesn't cover. It then commits via the existing self-heal
path (splice synthesized narration segments + stamp) so the settled timeline can
render the interleaved Chinese.

Each assertion is paired with a NEUTER reproducing the exact all-English symptom.
"""

import json
import logging

import pytest

import lib.translate.runtime as rt
import lib.translate.segment_backfill as sb

pytestmark = pytest.mark.unit


def _fake_tf(text, system_prompt, source='', target='', **kw):
    """Deterministic fake translate: prefix so output != input but derivable."""
    return ('ZH:' + text), {'_dispatch': {'model': 'fake-mt'}}


class _Row(dict):
    pass


class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeConn:
    """In-memory conversations row shared by the segment/message read AND the
    commit CAS write, so the backfill runs end-to-end and its stamp is
    observable on the stored message."""

    def __init__(self, conv_id, messages):
        self.conv_id = conv_id
        self.messages_json = json.dumps(messages)
        self.updated_at = 1000
        self.rev = 7
        self._pending = None

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        # The commit path (_commit_translation_inner) selects 3 cols + user_id
        # and CASes on rev; _read_message selects messages + user_id.
        if s.startswith('SELECT messages, updated_at, rev FROM conversations'):
            self._pending = ('sel_mur', None)
            return self
        if s.startswith('SELECT messages, updated_at FROM conversations'):
            self._pending = ('sel_mu', None)
            return self
        if s.startswith('SELECT messages FROM conversations'):
            self._pending = ('sel_m', None)
            return self
        if s.startswith('SELECT rev FROM conversations'):
            self._pending = ('sel_rev', None)
            return self
        if s.startswith('UPDATE conversations SET messages'):
            # Real query: SET messages=?, updated_at=? WHERE id=? AND user_id=? AND rev=?
            new_messages, new_updated, _cid, _uid, cas_rev = params
            if cas_rev != self.rev:
                return _Cursor(0)
            self.messages_json = new_messages
            self.updated_at = new_updated
            self.rev = self.rev + 1  # trigger would bump; the commit resets it
            return _Cursor(1)
        self._pending = ('other', None)
        return self

    def fetchone(self):
        kind = (self._pending or ('none', None))[0]
        if kind == 'sel_mur':
            return _Row(messages=self.messages_json,
                        updated_at=self.updated_at, rev=self.rev)
        if kind == 'sel_mu':
            return _Row(messages=self.messages_json, updated_at=self.updated_at)
        if kind == 'sel_m':
            return _Row(messages=self.messages_json)
        if kind == 'sel_rev':
            return _Row(rev=self.rev)
        return None

    def commit(self):
        pass


def _segmentless_msg(*, partial_by_round=None):
    """A finished agent turn shaped like the reported production row: NO
    ``segments`` array, only ``toolRounds`` with per-round English
    ``assistantContent`` narration on rounds 0 and 2 (rounds 1/3 are tool-only,
    no narration). The deliverable is already Chinese, so there is no
    ``translatedContent`` — mirroring the ``already in target language`` skip."""
    tool_rounds = [
        {'roundNum': 0, 'llmRound': 0, 'toolName': 'read_files',
         'assistantContent': 'I will investigate the myday feature.'},
        {'roundNum': 1, 'llmRound': 1, 'toolName': 'grep_search',
         'assistantContent': ''},
        {'roundNum': 2, 'llmRound': 2, 'toolName': 'read_files',
         'assistantContent': 'The historical balance is the cost data.'},
        {'roundNum': 3, 'llmRound': 3, 'toolName': 'run_command'},
    ]
    msg = {'role': 'assistant',
           'content': '我把归因逻辑读完了，给你一个确定的答案。',
           '_msgId': 'm1', '_taskId': 'task-xyz',
           'toolRounds': tool_rounds}
    if partial_by_round is not None:
        msg['_translatePartialByRound'] = partial_by_round
    return msg


def _install(monkeypatch, conn):
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain=None: conn)
    import lib.push as _push
    monkeypatch.setattr(_push, 'push_event', lambda *a, **k: None, raising=False)


def _narration_translated_count(stored_msg):
    return sum(1 for s in stored_msg.get('segments', [])
               if isinstance(s, dict) and s.get('type') == 'text'
               and not s.get('deliverable') and (s.get('translatedText') or '').strip())


# ── The reported symptom: segment-less turn, narration in toolRounds ─────────

def test_segmentless_turn_narration_backfilled_from_toolrounds(monkeypatch):
    """★ THE reported bug: msg has NO segments, only toolRounds with English
    assistantContent on rounds 0 and 2. The backfill must synthesise narration
    segments from toolRounds and stamp their Chinese so the settled timeline can
    render it — turning an all-English, translation-invisible turn into a
    translated one."""
    conn = _FakeConn('conv-sl', [_segmentless_msg()])
    _install(monkeypatch, conn)

    stored_before = json.loads(conn.messages_json)[0]
    assert 'segments' not in stored_before, 'fixture must start segment-less'

    stamped = sb.backfill_message_narration_sync(
        'conv-sl', 0, 'm1', 'Chinese', source='English')

    assert stamped == 2, 'only the two narration-bearing rounds (0,2) translate'
    stored = json.loads(conn.messages_json)[0]
    assert _narration_translated_count(stored) == 2, 'all-English → 2 rounds Chinese'
    # The synthesised narration segments carry the right per-round Chinese.
    by_round = {s.get('llmRound'): s.get('translatedText')
                for s in stored['segments']
                if s.get('type') == 'text' and not s.get('deliverable')}
    assert by_round.get(0) == 'ZH:I will investigate the myday feature.'
    assert by_round.get(2) == 'ZH:The historical balance is the cost data.'
    # The deliverable (already Chinese) is never given a translatedContent.
    assert 'translatedContent' not in stored, \
        'stamp-only must not fabricate a translatedContent for an already-target deliverable'


def test_reuses_translate_partial_by_round_zero_llm(monkeypatch):
    """★ Zero-LLM reuse: the Chinese for the rounds is ALREADY on the message in
    _translatePartialByRound (the live incremental worker persisted it before the
    turn was flagged already-Chinese). The backfill must reuse it verbatim and
    make NO translate call."""
    calls = {'n': 0}

    def _counting_tf(text, sp, source='', target='', **kw):
        calls['n'] += 1
        return ('ZH:' + text), {'_dispatch': {'model': 'fake-mt'}}

    monkeypatch.setattr(rt, '_translate_freetext', _counting_tf)
    import lib.database as _db
    import lib.push as _push
    conn = _FakeConn('conv-reuse', [_segmentless_msg(partial_by_round={
        '0': '我将调查“我的一天”功能。',
        '2': '历史余额指的是成本数据。',
    })])
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain=None: conn)
    monkeypatch.setattr(_push, 'push_event', lambda *a, **k: None, raising=False)

    stamped = sb.backfill_message_narration_sync(
        'conv-reuse', 0, 'm1', 'Chinese', source='English')

    assert stamped == 2
    assert calls['n'] == 0, 'reused _translatePartialByRound must cost ZERO LLM calls'
    stored = json.loads(conn.messages_json)[0]
    by_round = {s.get('llmRound'): s.get('translatedText')
                for s in stored['segments']
                if s.get('type') == 'text' and not s.get('deliverable')}
    assert by_round.get(0) == '我将调查“我的一天”功能。'
    assert by_round.get(2) == '历史余额指的是成本数据。'


def test_neuter_segmentless_stays_all_english(monkeypatch):
    """NEUTER: disable the toolRounds synthesis (mimic the pre-fix world where a
    segment-less turn produced no narration map). The turn stays translation-
    invisible — no segments stamped — proving the toolRounds path is
    load-bearing, not incidentally covered."""
    conn = _FakeConn('conv-sl', [_segmentless_msg()])
    _install(monkeypatch, conn)
    monkeypatch.setattr(sb, '_narration_map_from_tool_rounds', lambda *a, **k: {})

    stamped = sb.backfill_message_narration_sync(
        'conv-sl', 0, 'm1', 'Chinese', source='English')

    assert stamped == 0, 'neutered toolRounds path reproduces the all-English bug'
    stored = json.loads(conn.messages_json)[0]
    assert _narration_translated_count(stored) == 0


def test_has_untranslated_narration_fires_for_segmentless_toolrounds():
    """The candidate gate must recognise a segment-less turn whose toolRounds
    carry untranslated narration — otherwise the backfill is never even spawned
    (the invisibility total-gate)."""
    # Segment-less + toolRounds narration + no per-round translation → candidate.
    assert sb.has_untranslated_narration(_segmentless_msg()) is True
    # Segment-less + every narration round already in _translatePartialByRound
    # → NOT a candidate (nothing left to do).
    covered = _segmentless_msg(partial_by_round={'0': 'x', '2': 'y'})
    assert sb.has_untranslated_narration(covered) is False
    # Segment-less + toolRounds with NO narration at all → not a candidate.
    tool_only = {'role': 'assistant', 'content': 'x', '_msgId': 'm',
                 'toolRounds': [{'roundNum': 0, 'llmRound': 0, 'toolName': 'x'}]}
    assert sb.has_untranslated_narration(tool_only) is False
    # No segments AND no toolRounds → not a candidate.
    assert sb.has_untranslated_narration(
        {'role': 'assistant', 'content': 'x'}) is False


def test_warning_logged_when_toolrounds_but_no_segments(monkeypatch, caplog):
    """Traceability (owner requirement): when a turn has toolRounds but no
    segments — the exact 'invisible narration' shape — a WARNING must be emitted
    carrying conv_id / msg_idx / _taskId so the next occurrence is one grep away,
    not a DB dig."""
    conn = _FakeConn('conv-log', [_segmentless_msg()])
    _install(monkeypatch, conn)

    with caplog.at_level(logging.WARNING, logger='lib.translate.segment_backfill'):
        sb.backfill_message_narration_sync('conv-log', 0, 'm1', 'Chinese')

    hits = [r for r in caplog.records
            if 'toolRounds' in r.getMessage() and 'no segments' in r.getMessage().lower()]
    assert hits, 'expected a WARNING about a toolRounds-but-no-segments turn'
    msg = hits[0].getMessage()
    assert 'conv-log' in msg
    assert 'task-xyz' in msg, 'the _taskId must be in the log for traceability'
