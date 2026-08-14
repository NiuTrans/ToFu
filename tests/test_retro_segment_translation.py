"""Tests for the retro / on-open / manual / toggle translation path stamping
per-segment ``translatedText`` — symmetric with the live incremental worker.

Root cause fixed here: ``lib.translate.runtime._do_translate`` (the whole-message
path that runs when a COMPLETED conversation is opened, the global auto-translate
switch is toggled, or the user clicks Translate) used to commit ONLY the
deliverable ``translatedContent`` blob and never stamped ``translatedText`` onto
the narration segments. So the settled segment-timeline render fell back to the
English narration and all the Chinese collapsed to the bottom (the deliverable
blob), even though the STREAMING path interleaves it in place. This path now
BUILDS the ``{llmRound: 中文}`` map itself (translating each non-deliverable text
segment) and passes it into the existing
``_commit_translation_to_db(..., segment_translations=...)`` → the same
``_stamp_segment_translations`` plumbing the incremental worker uses.

These tests mirror ``test_incremental_translate.py``'s stamp assertion but drive
the whole-message/retro entry point. The LLM call is faked; a tiny in-memory
fake DB (shared by the segment read AND the commit's CAS write) makes the end-to-
end stamp observable without a real database.
"""

import copy
import json

import lib.translate.runtime as rt


def _fake_tf(text, system_prompt, source='', target='', **kw):
    """Deterministic fake translate: prefix so output != input but derivable."""
    return ('ZH:' + text), {'_dispatch': {'model': 'fake-mt'}}


class _Row(dict):
    """dict-like row supporting row['col'] as the real DB wrappers return."""


class _Cursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeConn:
    """In-memory conversations row shared by segment-read + commit CAS write.

    Handles exactly the two SELECTs and the one UPDATE the code under test
    issues, so ``_do_translate`` runs end-to-end and its stamp lands in the
    stored messages list (which the test reads back).
    """

    def __init__(self, conv_id, messages):
        self.conv_id = conv_id
        self.messages_json = json.dumps(messages)
        self.updated_at = 1000
        # The commit CAS token (lib/translate/commit.py): SELECT reads
        # ``messages, updated_at, rev`` and the UPDATE's WHERE is ``AND rev=?``
        # — ``updated_at`` is no longer the token (RENDER_CONTRACT Phase 4 W6;
        # the DB trigger is the sole rev bumper).
        self.rev = 7
        self._pending = None

    def execute(self, sql, params=()):
        s = ' '.join(sql.split())
        if s.startswith('SELECT messages, updated_at, rev FROM conversations'):
            self._pending = ('sel2', None)
            return self
        if s.startswith('SELECT messages FROM conversations'):
            self._pending = ('sel1', None)
            return self
        if s.startswith('SELECT rev FROM conversations'):
            self._pending = ('rev', None)
            return self
        if s.startswith('UPDATE conversations SET messages'):
            new_messages, new_updated = params[:2]
            cas_rev = params[-1]
            if cas_rev != self.rev:
                return _Cursor(0)               # CAS miss
            self.messages_json = new_messages
            self.updated_at = new_updated
            self.rev += 1                       # the trigger bumps rev on write
            return _Cursor(1)
        self._pending = ('other', None)
        return self

    def fetchone(self):
        kind = (self._pending or ('none', None))[0]
        if kind == 'sel2':
            return _Row(messages=self.messages_json, updated_at=self.updated_at,
                        rev=self.rev)
        if kind == 'sel1':
            return _Row(messages=self.messages_json)
        if kind == 'rev':
            return _Row(rev=self.rev)
        return None

    def begin(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass


def _segments():
    """A finished agent turn: two narration rounds + tools + a deliverable."""
    return [
        {'type': 'text', 'text': 'Let me read the files.', 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 't0', 'llmRound': 0},
        {'type': 'text', 'text': 'Now let me check the tests.', 'deliverable': False, 'llmRound': 1},
        {'type': 'tool_use', 'id': 't1', 'llmRound': 1},
        {'type': 'text', 'text': 'The final answer.', 'deliverable': True, 'terminal': True},
    ]


def _install(monkeypatch, conn, register_task=True):
    """Wire the fakes: LLM, push, thread DB, and a registered translate task."""
    monkeypatch.setattr(rt, '_translate_freetext', _fake_tf)
    import lib.database as _db
    monkeypatch.setattr(_db, 'get_thread_db', lambda domain=None: conn)
    import lib.database.conversation_repository as repo

    def _load(_db_conn, conv_id, **_kwargs):
        if conv_id != conn.conv_id:
            return None
        return repo.ConversationSnapshot(
            metadata={'rev': conn.rev},
            messages=json.loads(conn.messages_json),
            source='test_repository')

    def _replace(_db_conn, conv_id, messages, *, expected_rev=None, **_kwargs):
        if conv_id != conn.conv_id or expected_rev != conn.rev:
            return repo.ConversationWriteResult(applied=False, rev=None)
        conn.messages_json = json.dumps(copy.deepcopy(messages))
        conn.rev += 1
        return repo.ConversationWriteResult(applied=True, rev=conn.rev)

    monkeypatch.setattr(repo, 'load_conversation', _load)
    monkeypatch.setattr(repo, 'replace_messages', _replace)
    monkeypatch.setattr(repo, 'conversation_rows_authoritative', lambda: False)
    import lib.push as _push
    monkeypatch.setattr(_push, 'push_event', lambda *a, **k: None, raising=False)
    if register_task:
        with rt._translate_tasks_lock:
            rt._translate_tasks['task-retro'] = {
                'status': 'running', 'model': '', 'convId': conn.conv_id,
            }


# ── _build_segment_translation_map (the new helper) ──────────────────────────

def test_build_segment_map_translates_narration_by_llmround(monkeypatch):
    """The helper translates each NON-deliverable text segment and keys the
    result by llmRound. tool_use + the deliverable/terminal segment are
    excluded (the deliverable is rendered via translatedContent)."""
    conn = _FakeConn('conv-retro', [{'role': 'assistant', 'content': 'The final answer.',
                                     '_msgId': 'm1', 'segments': _segments()}])
    _install(monkeypatch, conn, register_task=False)

    seg_map = rt._build_segment_translation_map(
        'conv-retro', 'm1', 0, 'SYS', 'English', 'Chinese')

    assert seg_map == {0: 'ZH:Let me read the files.',
                       1: 'ZH:Now let me check the tests.'}, seg_map


def test_build_segment_map_skips_already_chinese(monkeypatch):
    """A narration segment already in the target language is kept verbatim
    (no wasted LLM call, no double-translation)."""
    zh = '让我先读取一下相关的文件，然后再检查测试用例。'
    segs = [
        {'type': 'text', 'text': zh, 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 't0', 'llmRound': 0},
    ]
    conn = _FakeConn('c', [{'role': 'assistant', 'content': 'x', '_msgId': 'm1', 'segments': segs}])
    _install(monkeypatch, conn, register_task=False)

    seg_map = rt._build_segment_translation_map('c', 'm1', 0, 'SYS', 'English', 'Chinese')
    assert seg_map == {0: zh}


def test_build_segment_map_noop_without_segments(monkeypatch):
    """A pre-v36 message (no segments) → None, never raises."""
    conn = _FakeConn('c', [{'role': 'assistant', 'content': 'plain english reply', '_msgId': 'm1'}])
    _install(monkeypatch, conn, register_task=False)
    assert rt._build_segment_translation_map('c', 'm1', 0, 'SYS', 'English', 'Chinese') is None


# ── End-to-end: _do_translate stamps translatedText onto stored segments ─────

def test_do_translate_stamps_translatedText_end_to_end(monkeypatch):
    """★ The reported fix: running the whole-message/retro path over a
    completed conversation produces INTERLEAVED translatedText on the narration
    segments (not just the bottom translatedContent blob)."""
    conn = _FakeConn('conv-retro', [{'role': 'assistant', 'content': 'The final answer.',
                                     '_msgId': 'm1', 'segments': _segments()}])
    _install(monkeypatch, conn)

    rt._do_translate('task-retro', 'The final answer.', 'Chinese', 'English',
                     'conv-retro', 0, 'translatedContent', msg_id='m1')

    stored = json.loads(conn.messages_json)[0]
    # The deliverable blob still committed.
    assert stored['translatedContent'] == 'ZH:The final answer.'
    segs = stored['segments']
    # Narration segments now carry their Chinese IN PLACE (interleaved render).
    assert segs[0]['translatedText'] == 'ZH:Let me read the files.'
    assert segs[2]['translatedText'] == 'ZH:Now let me check the tests.'
    # tool_use + deliverable/terminal segment are NOT stamped.
    assert 'translatedText' not in segs[1]
    assert 'translatedText' not in segs[3]
    assert 'translatedText' not in segs[4], \
        'deliverable/terminal segment must not be stamped (rendered via translatedContent)'


def test_do_translate_noop_stamp_for_pre_v36_message(monkeypatch):
    """Guardrail: a message WITHOUT segments still commits translatedContent
    and never invents a segments list (no-op stamp)."""
    conn = _FakeConn('conv-old', [{'role': 'assistant', 'content': 'plain english reply',
                                   '_msgId': 'm1'}])
    _install(monkeypatch, conn)

    rt._do_translate('task-retro', 'plain english reply', 'Chinese', 'English',
                     'conv-old', 0, 'translatedContent', msg_id='m1')

    stored = json.loads(conn.messages_json)[0]
    assert stored['translatedContent'] == 'ZH:plain english reply'
    assert 'segments' not in stored


def test_neuter_without_segment_map_reproduces_bottom_cluster(monkeypatch):
    """NEUTER: force the retro path to skip building the segment map (the
    pre-fix behaviour). The deliverable translatedContent still commits, but
    the narration segments get NO translatedText → the settled render falls
    back to English narration + the Chinese collapses to the bottom blob. This
    proves the segment-map build is the load-bearing part of the fix."""
    conn = _FakeConn('conv-retro', [{'role': 'assistant', 'content': 'The final answer.',
                                     '_msgId': 'm1', 'segments': _segments()}])
    _install(monkeypatch, conn)
    # Neuter the shipped helper → returns None, mimicking the old code that
    # never built the map.
    monkeypatch.setattr(rt, '_build_segment_translation_map', lambda *a, **k: None)

    rt._do_translate('task-retro', 'The final answer.', 'Chinese', 'English',
                     'conv-retro', 0, 'translatedContent', msg_id='m1')

    stored = json.loads(conn.messages_json)[0]
    assert stored['translatedContent'] == 'ZH:The final answer.'  # blob still lands
    segs = stored['segments']
    assert 'translatedText' not in segs[0], \
        'neutered path must NOT stamp narration — this is the bottom-cluster regression'
    assert 'translatedText' not in segs[2]
