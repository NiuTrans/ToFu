"""Tests for tests/_migrate_backfill_segment_translations.py — the one-shot
backfill that stamps ``translatedText`` onto ALREADY-translated historical
conversations so their narration segments interleave in place.

Covers:
  • eligibility filter (_needs_segment_translation) — only already-translated
    assistant messages with un-stamped narration segments qualify;
  • single-source-of-truth reuse — the migration binds the SAME translate core
    (runtime._translate_segments_to_map) + stamp helper
    (commit._stamp_segment_translations) the live retro path uses, no fork;
  • end-to-end translate → stamp → IDEMPOTENT re-run (0 on the second pass);
  • rev-neutral write (messages UPDATE guarded on rev, then rev reset — updated_at
    untouched; CAS-miss on a concurrent rev move → no write);
  • a biting NEUTER of the enrich-only guard.

The migration module is loaded by path (its filename starts with ``_migrate`` so
it isn't a normal importable test module). The LLM call is faked; the async DB
transaction is a tiny in-memory fake so the rev-neutral write is observable.
"""

import asyncio
import importlib.util
import json
import os

import lib.translate.runtime as rt

_MIG_PATH = os.path.join(os.path.dirname(__file__),
                         '_migrate_backfill_segment_translations.py')


def _load_migration():
    spec = importlib.util.spec_from_file_location('_seg_xlate_mig', _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_migration()


def _fake_tf(text, system_prompt, source='', target='', **kw):
    return ('ZH:' + text), {'_dispatch': {'model': 'fake-mt'}}


def _msg(translated_content='ZH:answer', stamped=False):
    """An already-translated assistant message with two narration segments."""
    seg0 = {'type': 'text', 'text': 'Let me read the files.',
            'deliverable': False, 'llmRound': 0}
    seg1 = {'type': 'text', 'text': 'Now let me check the tests.',
            'deliverable': False, 'llmRound': 1}
    if stamped:
        seg0['translatedText'] = 'ZH:Let me read the files.'
        seg1['translatedText'] = 'ZH:Now let me check the tests.'
    return {
        'role': 'assistant', 'content': 'The answer.',
        'translatedContent': translated_content, '_msgId': 'm1',
        'segments': [
            seg0,
            {'type': 'tool_use', 'id': 't0', 'llmRound': 0},
            seg1,
            {'type': 'tool_use', 'id': 't1', 'llmRound': 1},
            {'type': 'text', 'text': 'The answer.', 'deliverable': True, 'terminal': True},
        ],
    }


# ── Eligibility filter ───────────────────────────────────────────────────────

def test_eligible_already_translated_with_unstamped_narration():
    assert mig._needs_segment_translation(_msg()) is True


def test_not_eligible_when_not_yet_translated():
    """No translatedContent → the message hasn't been translated at all; the
    backfill ENRICHES existing translations, it does not initiate them."""
    m = _msg()
    m['translatedContent'] = ''
    assert mig._needs_segment_translation(m) is False


def test_not_eligible_when_all_narration_already_stamped():
    assert mig._needs_segment_translation(_msg(stamped=True)) is False


def test_not_eligible_pre_v36_no_segments():
    m = _msg()
    m.pop('segments')
    assert mig._needs_segment_translation(m) is False


def test_not_eligible_user_message():
    m = _msg()
    m['role'] = 'user'
    assert mig._needs_segment_translation(m) is False


# ── Single source of truth (no forked logic) ────────────────────────────────

def test_migration_reuses_live_translate_core_and_stamp_helper():
    """The migration must bind the SAME translate core + stamp helper the
    render-feeding write path uses — a divergent copy would drift."""
    from lib.translate.commit import _stamp_segment_translations
    from lib.translate.runtime import _translate_segments_to_map
    assert mig._translate_segments_to_map is _translate_segments_to_map
    assert mig._stamp_segment_translations is _stamp_segment_translations


# ── End-to-end translate → stamp → idempotency (pure, no DB) ─────────────────

def test_translate_core_stamps_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)
    m = _msg()
    seg_map = rt._translate_segments_to_map(m['segments'], 'SYS', '', 'Chinese')
    assert seg_map == {0: 'ZH:Let me read the files.',
                       1: 'ZH:Now let me check the tests.'}, seg_map
    mig._stamp_segment_translations(m, seg_map)
    assert m['segments'][0]['translatedText'] == 'ZH:Let me read the files.'
    assert m['segments'][2]['translatedText'] == 'ZH:Now let me check the tests.'
    # tool_use + deliverable/terminal never stamped.
    assert 'translatedText' not in m['segments'][1]
    assert 'translatedText' not in m['segments'][4]

    # ── IDEMPOTENT: a second pass finds every narration already stamped →
    #    enrich-only core returns {} → nothing to re-translate. ──
    seg_map2 = rt._translate_segments_to_map(m['segments'], 'SYS', '', 'Chinese')
    assert seg_map2 == {}, seg_map2


def test_enrich_only_neuter_would_retranslate(monkeypatch):
    """NEUTER of the enrich-only guard: if the core did NOT skip segments that
    already carry translatedText, a second pass would re-translate them (and a
    live re-run would clobber a possibly-better existing translation). Proves
    the ``translatedText`` skip is the load-bearing idempotency guard."""
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)
    m = _msg(stamped=True)  # both narration segments already stamped
    # Correct (shipped) behaviour: nothing to do.
    assert rt._translate_segments_to_map(m['segments'], 'SYS', '', 'Chinese') == {}

    # Neutered core: drop the "already has translatedText → skip" line.
    def _neutered(segs, system_prompt, source, target, *, log_tag='?'):
        out = {}
        for seg in (segs or []):
            if not isinstance(seg, dict) or seg.get('type') != 'text' or seg.get('deliverable'):
                continue
            lr = seg.get('llmRound')
            if lr is None:
                continue
            original = (seg.get('text') or '').strip()
            if original:
                out[lr] = 'ZH:' + original  # re-translates unconditionally
        return out

    monkeypatch.setattr(rt, '_translate_segments_to_map', _neutered)
    assert rt._translate_segments_to_map(m['segments'], 'SYS', '', 'Chinese') != {}, \
        'neutered core re-translates already-stamped segments — enrich-only guard is load-bearing'


# ── Rev-neutral write ────────────────────────────────────────────────────────

class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeTxnConn:
    """Records the two UPDATE statements the rev-neutral write issues."""

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
                return _Cursor(0)              # CAS miss (concurrent writer)
            self.messages_written = _msgs
            self.rev_after = self.current_rev + 1  # trigger bumps rev
            return _Cursor(1)
        if s.startswith('UPDATE conversations SET rev='):
            new_rev, _cid = params
            self.rev_after = new_rev           # reset back to expected_rev
            return _Cursor(1)
        return _Cursor(0)


class _FakeTxnCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def test_rev_neutral_write_holds_rev_and_never_touches_updated_at(monkeypatch):
    conn = _FakeTxnConn(current_rev=7)
    monkeypatch.setattr(mig, 'async_transaction', lambda: _FakeTxnCtx(conn))

    wrote = asyncio.run(mig._rev_neutral_write('c1', '{"messages":1}', 7))
    assert wrote is True
    # messages UPDATE then rev-reset — exactly two statements, rev ends at 7.
    assert len(conn.calls) == 2
    assert conn.calls[0][0].startswith('UPDATE conversations SET messages=')
    assert conn.calls[1][0].startswith('UPDATE conversations SET rev=')
    assert conn.rev_after == 7, 'rev must be held at expected_rev, not bumped'
    # No statement touches updated_at → no sidebar re-sort / staleness flip.
    assert all('updated_at' not in c[0] for c in conn.calls)


def test_rev_neutral_write_skips_on_concurrent_rev_move(monkeypatch):
    conn = _FakeTxnConn(current_rev=7)
    monkeypatch.setattr(mig, 'async_transaction', lambda: _FakeTxnCtx(conn))

    # A concurrent writer moved rev 7 → 9; our CAS on rev=7 misses.
    wrote = asyncio.run(mig._rev_neutral_write('c1', '{"messages":1}', 9))
    assert wrote is False
    assert conn.messages_written is None, 'must NOT write on CAS miss'
    # Only the guarded messages UPDATE ran; the rev-reset never fired.
    assert len(conn.calls) == 1


# ── _as_list coercion (defensive) ────────────────────────────────────────────

def test_as_list_coerces_json_text_and_rejects_garbage():
    assert mig._as_list('[{"a":1}]') == [{'a': 1}]
    assert mig._as_list([{'a': 1}]) == [{'a': 1}]
    assert mig._as_list('not json') is None
    assert mig._as_list(None) is None
