"""Path-independent narration backfill — the ROOT-CAUSE regression for the
reported bug "only the final Assistant deliverable is translated; the tool
narration between two Users is not".

DIAGNOSIS (evidence-locked against the live DB, not just the test harness)
--------------------------------------------------------------------------
Narration-segment translation (``msg.segments[].translatedText``, rendered
interleaved with the tools it describes) was only ever a SIDE-EFFECT of the
whole-message LLM branch actually running. The DELIVERABLE (``translatedContent``)
is settled by MANY terminal paths, but three of them return WITHOUT ever
building/stamping the narration map:

  • Path A — the incremental accumulator "owns" the turn: ``_do_finalize_inner``
    stamps ``seg_trans`` ONLY from ``self.segments`` (rounds it translated LIVE
    during the task). A late-started / partially-reclaimed accumulator, or a
    round whose ``_translate_segment`` raised, commits the deliverable but leaves
    DB narration segments with empty ``translatedText``.
  • Path B1 — the whole-message safety net's ``already has translatedContent``
    early-return: the deliverable is already translated, so it returns BEFORE
    building the segment map.
  • Path B2 — the ``content already in target language`` early-return: same, and
    there is not even a ``translatedContent`` (the deliverable was already in the
    target language) yet the English narration is untranslated.

THE FIX makes the narration stamp a FIRST-CLASS, path-independent terminal step
via ``lib.translate.segment_backfill.backfill_message_narration_sync`` (reads DB
segments → builds the {llmRound: 中文} map for any segment still missing
``translatedText`` via the SHARED enrich-only core → commits a STAMP-ONLY
``field=None`` write that leaves ``translatedContent``/``content`` untouched).

Each assertion below is paired with a NEUTER that disables the mechanism and
reproduces the exact 0/N symptom, proving the DB backfill is load-bearing (not
incidentally covered by a live cache).
"""

import json

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
    """In-memory conversations row shared by the segment read AND the commit
    CAS write, so the backfill runs end-to-end and its stamp is observable.

    Mirrors the real production message shape: an assistant turn with N
    non-deliverable narration text segments (all with an empty translatedText),
    interleaved tool_use segments, and a deliverable terminal text segment.
    """

    def __init__(self, conv_id, messages):
        self.conv_id = conv_id
        self.messages_json = json.dumps(messages)
        self.updated_at = 1000
        self.rev = 7
        self._pending = None

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
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
            new_messages, new_updated, _cid, _uid, cas_updated = params
            if cas_updated != self.updated_at:
                return _Cursor(0)               # CAS miss
            self.messages_json = new_messages
            self.updated_at = new_updated
            return _Cursor(1)
        self._pending = ('other', None)
        return self

    def fetchone(self):
        kind = (self._pending or ('none', None))[0]
        if kind == 'sel_mu':
            return _Row(messages=self.messages_json, updated_at=self.updated_at)
        if kind == 'sel_m':
            return _Row(messages=self.messages_json)
        if kind == 'sel_rev':
            return _Row(rev=self.rev)
        return None

    def commit(self):
        pass


def _msg_with_untranslated_narration(n=13, *, translated_deliverable=True):
    """A finished agent turn shaped like the real production DB rows:
    ``n`` narration text segments (translatedText EMPTY), interleaved tool_use,
    and a deliverable terminal text segment. This is the reported symptom's
    exact shape: deliverable translated, narration all-English (0/n)."""
    segs = []
    for i in range(n):
        segs.append({'type': 'text', 'text': f'Narration round {i}.',
                     'deliverable': False, 'llmRound': i})
        segs.append({'type': 'tool_use', 'id': f't{i}', 'llmRound': i})
    segs.append({'type': 'text', 'text': 'The final answer.',
                 'deliverable': True, 'terminal': True})
    msg = {'role': 'assistant', 'content': 'The final answer.',
           '_msgId': 'm1', 'segments': segs}
    if translated_deliverable:
        msg['translatedContent'] = 'ZH:The final answer.'
        msg['_translateDone'] = True
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


# ── The reported symptom, reproduced on real DB shape ────────────────────────

def test_backfill_turns_0_of_13_into_13_of_13(monkeypatch):
    """★ THE reported bug: a turn with a translated deliverable but 13 narration
    segments all missing translatedText (0/13). The path-independent backfill
    stamps every one (13/13) and leaves translatedContent untouched."""
    conn = _FakeConn('conv-prod', [_msg_with_untranslated_narration(13)])
    _install(monkeypatch, conn)

    before = _narration_translated_count(json.loads(conn.messages_json)[0])
    assert before == 0, 'fixture must start at the reported 0/13 symptom'

    stamped = sb.backfill_message_narration_sync(
        'conv-prod', 0, 'm1', 'Chinese', source='English')

    assert stamped == 13
    stored = json.loads(conn.messages_json)[0]
    assert _narration_translated_count(stored) == 13, '0/13 → 13/13'
    # Deliverable blob is UNTOUCHED (stamp-only commit; field=None).
    assert stored['translatedContent'] == 'ZH:The final answer.'
    # A specific narration segment now carries its Chinese in place.
    assert stored['segments'][0]['translatedText'] == 'ZH:Narration round 0.'
    # Deliverable/terminal + tool_use segments are never stamped.
    assert 'translatedText' not in stored['segments'][-1]
    assert 'translatedText' not in stored['segments'][1]


def test_neuter_without_db_backfill_stays_0_of_13(monkeypatch):
    """NEUTER: force the shared builder to return an empty map (mimicking the
    pre-fix world where no path built the narration map from the DB). The
    deliverable stays translated but narration stays 0/13 — the exact reported
    symptom — proving the DB backfill is load-bearing."""
    conn = _FakeConn('conv-prod', [_msg_with_untranslated_narration(13)])
    _install(monkeypatch, conn)
    monkeypatch.setattr(rt, '_build_segment_translation_map', lambda *a, **k: None)

    stamped = sb.backfill_message_narration_sync(
        'conv-prod', 0, 'm1', 'Chinese', source='English')

    assert stamped == 0
    stored = json.loads(conn.messages_json)[0]
    assert _narration_translated_count(stored) == 0, \
        'neutered path reproduces the reported 0/13 bug'


def test_backfill_is_idempotent_enrich_only(monkeypatch):
    """A re-run over an already-stamped turn does NO work (enrich-only): the
    shared core returns {} → stamp-only commit is skipped → 0 stamped, and the
    already-present translations are untouched."""
    conn = _FakeConn('conv-prod', [_msg_with_untranslated_narration(13)])
    _install(monkeypatch, conn)

    first = sb.backfill_message_narration_sync('conv-prod', 0, 'm1', 'Chinese')
    assert first == 13
    second = sb.backfill_message_narration_sync('conv-prod', 0, 'm1', 'Chinese')
    assert second == 0, 'idempotent: nothing left to translate on re-run'
    assert _narration_translated_count(json.loads(conn.messages_json)[0]) == 13


def test_stamp_only_commit_leaves_deliverable_when_no_translatedContent(monkeypatch):
    """Path B2 shape: the deliverable was already in the target language, so the
    turn has NO translatedContent. The backfill still stamps the English
    narration and MUST NOT invent a translatedContent."""
    msg = _msg_with_untranslated_narration(3, translated_deliverable=False)
    conn = _FakeConn('conv-tgt', [msg])
    _install(monkeypatch, conn)

    stamped = sb.backfill_message_narration_sync('conv-tgt', 0, 'm1', 'Chinese')

    assert stamped == 3
    stored = json.loads(conn.messages_json)[0]
    assert _narration_translated_count(stored) == 3
    assert 'translatedContent' not in stored, \
        'stamp-only (field=None) must never fabricate a translatedContent'


# ── has_untranslated_narration predicate (the cheap pre-check gate) ──────────

def test_predicate_fires_for_untranslated_narration_only():
    """The gate that decides whether to spawn a backfill: True iff a narration
    text segment is missing translatedText — regardless of translatedContent."""
    # 0/13, deliverable translated → candidate.
    assert sb.has_untranslated_narration(_msg_with_untranslated_narration(2)) is True
    # 0/n, deliverable NOT translated (already-target case) → still a candidate.
    assert sb.has_untranslated_narration(
        _msg_with_untranslated_narration(2, translated_deliverable=False)) is True
    # Fully stamped → not a candidate.
    fully = _msg_with_untranslated_narration(2)
    for s in fully['segments']:
        if s.get('type') == 'text' and not s.get('deliverable'):
            s['translatedText'] = 'ZH:x'
    assert sb.has_untranslated_narration(fully) is False
    # No segments (pre-v36) → not a candidate.
    assert sb.has_untranslated_narration(
        {'role': 'assistant', 'content': 'x', 'translatedContent': 'y'}) is False


# ── Path A: incremental-owned finalize backfills from the DB ─────────────────

def test_incremental_finalize_backfills_missing_rounds_from_db(monkeypatch):
    """Path A: the incremental accumulator finalized the deliverable but its
    LIVE cache only covered SOME rounds (the rest never translated live — late
    start / reclaim / per-segment error). The finalize's DB backfill must fill
    the missing narration from the DB, not only the live cache."""
    import lib.translate.incremental as inc

    # DB already has the settled turn (deliverable translated, 0/5 narration) —
    # this is what _sync_result_to_conversation persisted.
    conn = _FakeConn('conv-incr', [_msg_with_untranslated_narration(5)])
    _install(monkeypatch, conn)

    # Build an accumulator whose LIVE cache is EMPTY (simulating rounds that
    # never got translated live) and drive its finalize inner directly.
    task = {'id': 'tA', 'convId': 'conv-incr', 'config': {}, 'status': 'running'}
    acc = inc._Acc(task)
    try:
        # Empty live cache → deliverable resolved via whole-content fallback,
        # narration seg_trans is {} → without the DB backfill this would be 0/5.
        from lib.translate.engine import _translate_freetext as _real  # noqa
        from lib.translate.notranslate import (_extract_notranslate_blocks,
                                               _reattach_notranslate_blocks)
        from lib.translate.prompt import _build_translate_prompt
        sp = _build_translate_prompt('Chinese', 'English')
        acc._do_finalize_inner('conv-incr', 0, 'The final answer.', 'm1', sp,
                               _fake_tf, _extract_notranslate_blocks,
                               _reattach_notranslate_blocks)
    finally:
        acc._cleanup()

    stored = json.loads(conn.messages_json)[0]
    assert _narration_translated_count(stored) == 5, \
        'incremental finalize must DB-backfill narration its live cache missed'


def test_neuter_incremental_without_db_backfill_stays_zero(monkeypatch):
    """NEUTER for Path A: disable the finalize DB backfill → the empty live
    cache leaves narration at 0/5 (the reported bug on the incremental path)."""
    import lib.translate.incremental as inc

    conn = _FakeConn('conv-incr', [_msg_with_untranslated_narration(5)])
    _install(monkeypatch, conn)
    monkeypatch.setattr(sb, 'backfill_message_narration_sync', lambda *a, **k: 0)

    task = {'id': 'tA2', 'convId': 'conv-incr', 'config': {}, 'status': 'running'}
    acc = inc._Acc(task)
    try:
        from lib.translate.notranslate import (_extract_notranslate_blocks,
                                               _reattach_notranslate_blocks)
        from lib.translate.prompt import _build_translate_prompt
        sp = _build_translate_prompt('Chinese', 'English')
        acc._do_finalize_inner('conv-incr', 0, 'The final answer.', 'm1', sp,
                               _fake_tf, _extract_notranslate_blocks,
                               _reattach_notranslate_blocks)
    finally:
        acc._cleanup()

    stored = json.loads(conn.messages_json)[0]
    assert _narration_translated_count(stored) == 0, \
        'neutered incremental path reproduces 0/5'
