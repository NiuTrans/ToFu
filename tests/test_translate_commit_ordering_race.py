#!/usr/bin/env python3
"""tests/test_translate_commit_ordering_race.py — the translatedText 0/N race.

ROOT-CAUSE verification for the reported "after generation, all the translated
tool-call narration clumps into one block at the tail" bug.

Empirically confirmed on the real failing row (conv mrd64vv3vpfidf): the
message had 63 segments and a valid `translatedContent`, but 0/63 segments
carried `translatedText`. The cause was an ORDERING RACE:

  * the incremental finalize worker translates each round's narration in
    memory and hands the commit a ``{llmRound: 中文}`` map, then
  * ``_commit_translation_to_db`` re-reads the target message FROM THE DB
    inside its CAS loop and calls ``_stamp_segment_translations`` — but at
    that instant the conversation row had NO ``segments`` yet (the finalize
    commit raced ahead of / lost the row-write CAS to
    ``_sync_result_to_conversation``), so the stamp hit its ``if not segs:
    return`` guard and silently dropped all 19 narration translations.

The FIX (backend-as-SSOT): ``finalize_incremental`` captures the authoritative
thin ``task['segments']`` and hands them to the commit as ``fallback_segments``.
When the commit resolves a DB message that lacks ``segments`` but has a
non-empty translation map + the fallback, it SPLICES the authoritative segments
onto the message in the SAME CAS write, then stamps — so the per-round
translations can never be dropped onto a segment-less row again.

These tests drive the REAL ``_commit_translation_to_db`` against a real
``conversations`` row (seeded segment-less, exactly the racing state):
  * POSITIVE: with ``fallback_segments`` supplied, the persisted row ends up
    WITH segments and the narration segments carry ``translatedText`` (0→N).
  * NEUTER: drop ``fallback_segments`` (the pre-fix behaviour) and the same
    commit leaves the row segment-less with 0 stamped — reproducing the bug.
  * GATING: a plain ``translatedContent`` commit (no map) must NOT fabricate
    segments onto a segment-less row.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/xlate_race.db \
        python3 tests/test_translate_commit_ordering_race.py
or via pytest (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1).
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/xlate_race_unittest.db')

import pytest

pytestmark = pytest.mark.unit

# The authoritative thin segments the backend assembles for the turn — the
# shape segments_to_json(task['segments']) produces (2 narration rounds + a
# tool_use each + the terminal deliverable).
_THIN_SEGMENTS = [
    {'type': 'thinking', 'text': 'reason0', 'deliverable': False, 'llmRound': 0},
    {'type': 'text', 'text': 'Let me read the files.', 'deliverable': False, 'llmRound': 0},
    {'type': 'tool_use', 'id': 'tc0', 'name': 'read_files', 'input': '{}', 'llmRound': 0},
    {'type': 'text', 'text': 'Now let me search.', 'deliverable': False, 'llmRound': 1},
    {'type': 'tool_use', 'id': 'tc1', 'name': 'web_search', 'input': '{}', 'llmRound': 1},
    {'type': 'text', 'text': 'The final answer.', 'deliverable': True, 'terminal': True},
]

# The per-round translation map the incremental worker would hand the commit
# (keyed by llmRound ≡ round_num; only non-deliverable narration rounds).
_SEG_TRANS = {0: '让我读取文件。', 1: '现在让我搜索。'}

_MSG_ID = 'm-race-asst'


def _seed_segmentless_conv(conv_id):
    """Insert a conversations row whose assistant message has NO `segments` —
    the exact racing state (segment write hasn't landed / lost the CAS)."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    messages = [
        {'role': 'user', 'content': 'q', '_msgId': 'm-race-user', 'timestamp': now},
        {'role': 'assistant', 'content': 'The final answer.', '_msgId': _MSG_ID,
         '_taskId': 'task-race', 'timestamp': now + 1},  # ← NO 'segments' key
    ]
    db = get_thread_db(DOMAIN_CHAT)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'xlate-race',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_asst(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    for m in reversed(msgs):
        if m.get('role') == 'assistant':
            return m
    return None


def _stamped_rounds(msg):
    segs = (msg or {}).get('segments') or []
    return sorted(s.get('llmRound') for s in segs
                  if isinstance(s, dict) and (s.get('translatedText') or '').strip())


def _cleanup(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


class TestTranslateCommitOrderingRace(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-xlate-race-' + str(id(self))
        _cleanup(self.conv_id)
        _seed_segmentless_conv(self.conv_id)

    def tearDown(self):
        _cleanup(self.conv_id)

    def test_fallback_segments_self_heal_stamps_narration(self):
        """POSITIVE: the DB msg has no segments (the race), but the commit is
        handed the authoritative fallback_segments + the map → it splices the
        segments on and stamps the narration in the SAME CAS write. 0→2."""
        from lib.translate.commit import _commit_translation_to_db

        before = _read_asst(self.conv_id)
        self.assertNotIn('segments', before,
                         'precondition: DB message must be segment-less (the race)')

        _commit_translation_to_db(
            self.conv_id, 1, 'translatedContent', '最终答案。',
            original_text='The final answer.', model='fake-mt', msg_id=_MSG_ID,
            segment_translations=dict(_SEG_TRANS),
            fallback_segments=[dict(s) for s in _THIN_SEGMENTS])

        after = _read_asst(self.conv_id)
        self.assertEqual(after.get('translatedContent'), '最终答案。',
                         'deliverable translatedContent must still commit')
        segs = after.get('segments')
        self.assertIsInstance(segs, list)
        self.assertEqual(len(segs), len(_THIN_SEGMENTS),
                         'authoritative segments must be spliced onto the row')
        self.assertEqual(_stamped_rounds(after), [0, 1],
                         'both narration rounds must carry translatedText (0→2)')
        # The deliverable/terminal + tool_use segments must NOT be stamped.
        deliv = [s for s in segs if s.get('type') == 'text' and s.get('deliverable')]
        self.assertEqual(len(deliv), 1)
        self.assertNotIn('translatedText', deliv[0])

    def test_neuter_no_fallback_reproduces_zero_stamp(self):
        """NEUTER of the fix: WITHOUT fallback_segments (the pre-fix call), the
        commit resolves a segment-less DB msg → _stamp_segment_translations
        no-ops → row stays segment-less, 0 stamped. Reproduces the exact bug
        (translatedContent lands, per-round narration is dropped)."""
        from lib.translate.commit import _commit_translation_to_db

        _commit_translation_to_db(
            self.conv_id, 1, 'translatedContent', '最终答案。',
            original_text='The final answer.', model='fake-mt', msg_id=_MSG_ID,
            segment_translations=dict(_SEG_TRANS),
            fallback_segments=None)  # ← the pre-fix behaviour

        after = _read_asst(self.conv_id)
        self.assertEqual(after.get('translatedContent'), '最终答案。',
                         'translatedContent still commits (the survivor in the bug)')
        self.assertFalse(after.get('segments'),
                         'NEUTER: without fallback_segments the row stays '
                         'segment-less — this IS the 0/N bug')
        self.assertEqual(_stamped_rounds(after), [],
                         'NEUTER: 0 narration segments stamped (the reported bug)')

    def test_no_map_never_fabricates_segments(self):
        """GATING: a plain translatedContent commit (no segment_translations
        map) must NEVER splice segments onto a segment-less row — the self-heal
        is strictly map-gated, so segments stay backend-authoritative and are
        not invented by the translate path."""
        from lib.translate.commit import _commit_translation_to_db

        _commit_translation_to_db(
            self.conv_id, 1, 'translatedContent', '最终答案。',
            original_text='The final answer.', model='fake-mt', msg_id=_MSG_ID,
            segment_translations=None,
            fallback_segments=[dict(s) for s in _THIN_SEGMENTS])

        after = _read_asst(self.conv_id)
        self.assertEqual(after.get('translatedContent'), '最终答案。')
        self.assertFalse(after.get('segments'),
                         'no map → self-heal must not fire; segments must NOT '
                         'be fabricated by a plain translatedContent commit')

    def test_existing_segments_not_overwritten_by_fallback(self):
        """When the DB msg ALREADY has segments (the happy path — the row-write
        won the race), the fallback must NOT clobber them; the stamp lands on
        the existing segments. Guards against the self-heal replacing a richer
        real segment list with the thin fallback."""
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.translate.commit import _commit_translation_to_db

        # Rewrite the row WITH segments present.
        db = get_thread_db(DOMAIN_CHAT)
        msg = _read_asst(self.conv_id)
        real_segments = [dict(s) for s in _THIN_SEGMENTS]
        real_segments[1]['text'] = 'REAL round-0 narration.'  # mark distinct
        row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                         (self.conv_id,)).fetchone()
        msgs = json.loads(row[0])
        for m in msgs:
            if m.get('_msgId') == _MSG_ID:
                m['segments'] = real_segments
        db.execute('UPDATE conversations SET messages=? WHERE id=? AND user_id=1',
                   (json_dumps_pg(msgs), self.conv_id))
        db.commit()

        _commit_translation_to_db(
            self.conv_id, 1, 'translatedContent', '最终答案。',
            original_text='The final answer.', model='fake-mt', msg_id=_MSG_ID,
            segment_translations=dict(_SEG_TRANS),
            fallback_segments=[dict(s) for s in _THIN_SEGMENTS])

        after = _read_asst(self.conv_id)
        segs = after.get('segments')
        # The REAL narration text is preserved (fallback did NOT overwrite).
        r0 = next(s for s in segs if s.get('type') == 'text' and s.get('llmRound') == 0)
        self.assertEqual(r0['text'], 'REAL round-0 narration.',
                         'existing segments must not be clobbered by the fallback')
        self.assertEqual(_stamped_rounds(after), [0, 1],
                         'stamp still lands on the existing segments')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_translate_commit_ordering_race.__main__', init_schema=False)
    unittest.main(verbosity=2)
