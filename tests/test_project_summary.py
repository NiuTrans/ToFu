"""tests/test_project_summary.py — Layer 2 cross-conversation awareness.

Covers the lazy summary engine (lib/conversations/project_summary.py):
  - staleness gating (regenerate only on material msg_count growth)
  - settings-only persistence (messages/updated_at untouched)
  - SUMMARY_MIN_MESSAGES floor
  - bounded project digest (cap, self-exclusion, title fallback, summary use)

The cheap-model call is monkeypatched so tests are deterministic + offline.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.conversations.project_summary as ps


@pytest.mark.api
class TestProjectSummaryEngine:
    @pytest.fixture(autouse=True)
    def seed(self, flask_client, monkeypatch):
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database._core_schema import CONVERSATIONS, upsert

        self.db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        tag = f'{now}'
        self.proj = f'/tmp/projsum_{tag}'
        self.ids = []

        def _mk(cid, title, n_msgs, ts, project=None, summary=None):
            msgs = []
            for i in range(n_msgs):
                role = 'user' if i % 2 == 0 else 'assistant'
                msgs.append({'role': role, 'content': f'{role} message {i}'})
            settings = {'projectPath': project if project is not None else self.proj}
            if summary is not None:
                settings['projectSummary'] = {
                    'text': summary, 'generated_at': ts,
                    'msg_count_at_gen': n_msgs,
                }
            upsert(self.db, CONVERSATIONS, {
                'id': cid, 'user_id': 1, 'title': title,
                'messages': json.dumps(msgs), 'created_at': ts, 'updated_at': ts,
                'settings': json.dumps(settings), 'msg_count': n_msgs,
                'search_text': title,
            }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                            'updated_at', 'settings', 'msg_count', 'search_text'],
               retry=True)
            self.ids.append(cid)
            return cid

        self.c_fresh = _mk(f'psum-fresh-{tag}', 'Fresh', 8, now + 5,
                           summary='Existing cached summary.')
        self.c_grown = _mk(f'psum-grown-{tag}', 'Grown', 20, now + 4)  # no summary
        self.c_tiny = _mk(f'psum-tiny-{tag}', 'Tiny', 2, now + 3)      # below floor
        self.c_other = _mk(f'psum-other-{tag}', 'OtherProj', 8, now + 2,
                           project='/tmp/other_proj')

        # Stub the LLM so generation is deterministic + offline.
        self.calls = []

        def _fake_dispatch(messages, **kwargs):
            self.calls.append(messages)
            return ('GENERATED SUMMARY OK', {})

        monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', _fake_dispatch)
        yield
        for cid in self.ids:
            self.db.execute('DELETE FROM conversations WHERE id=?', (cid,))
        self.db.commit()

    def _settings(self, cid):
        row = self.db.execute(
            'SELECT settings, messages, updated_at FROM conversations WHERE id=?',
            (cid,)).fetchone()
        s = row['settings']
        return json.loads(s) if isinstance(s, str) else s, row

    def test_fresh_summary_not_regenerated(self):
        # Cached summary, msg_count unchanged → no LLM call, returns cached.
        out = ps.ensure_summary(self.c_fresh, blocking=True)
        assert out == 'Existing cached summary.'
        assert len(self.calls) == 0

    def test_missing_summary_is_generated_and_persisted(self):
        out = ps.ensure_summary(self.c_grown, blocking=True)
        assert out == 'GENERATED SUMMARY OK'
        assert len(self.calls) == 1
        settings, _ = self._settings(self.c_grown)
        assert settings['projectSummary']['text'] == 'GENERATED SUMMARY OK'
        assert settings['projectSummary']['msg_count_at_gen'] == 20

    def test_below_min_messages_skipped(self):
        out = ps.ensure_summary(self.c_tiny, blocking=True)
        assert out is None
        assert len(self.calls) == 0

    def test_persist_touches_only_settings(self):
        _, before = self._settings(self.c_grown)
        ps.ensure_summary(self.c_grown, blocking=True)
        _, after = self._settings(self.c_grown)
        # messages + updated_at must be unchanged by a summary write.
        assert before['messages'] == after['messages']
        assert before['updated_at'] == after['updated_at']

    def test_stale_on_material_growth(self):
        # A cached summary whose msg_count_at_gen is far below current → stale.
        stored = {'text': 'old', 'msg_count_at_gen': 8}
        assert ps._is_stale(stored, 8) is False           # no growth
        assert ps._is_stale(stored, 8 + ps.SUMMARY_STALE_GROWTH) is True
        assert ps._is_stale(None, 50) is True              # never summarized

    def test_force_regenerates_even_when_fresh(self):
        out = ps.ensure_summary(self.c_fresh, blocking=True, force=True)
        assert out == 'GENERATED SUMMARY OK'
        assert len(self.calls) == 1

    def test_digest_bounded_and_scoped(self):
        d = ps.build_project_digest(self.proj, current_conv_id=self.c_fresh, limit=10)
        # Self excluded; other-project conv excluded; siblings present.
        assert self.c_fresh not in d
        assert self.c_other not in d
        assert self.c_grown in d
        assert self.c_tiny in d
        # The conv with a cached summary renders it; one without uses title only.
        assert 'Existing cached summary.' not in d  # that conv is the current one (excluded)

    def test_digest_uses_summary_text_when_present(self):
        # Add a sibling WITH a summary and confirm it shows in the digest body.
        d = ps.build_project_digest('/tmp/other_proj', limit=10)
        assert self.c_other in d  # only sibling in that project

    def test_digest_entries_structured_and_consistent_with_text(self):
        # The structured backbone the frontend chip consumes: same siblings,
        # same self/other-project exclusion, summary text carried through, and
        # consistent with what build_project_digest renders into the prompt.
        entries = ps.project_digest_entries(
            self.proj, current_conv_id=self.c_fresh, limit=10)
        ids = {e['id'] for e in entries}
        assert self.c_fresh not in ids          # self excluded
        assert self.c_other not in ids          # other project excluded
        assert self.c_grown in ids and self.c_tiny in ids
        # Each entry is a {id,title,summary} dict; summary='' when none cached.
        for e in entries:
            assert set(e.keys()) == {'id', 'title', 'summary'}
        # Consistency: every structured id appears in the rendered text digest.
        text = ps.build_project_digest(
            self.proj, current_conv_id=self.c_fresh, limit=10)
        for e in entries:
            assert e['id'] in text

    def test_digest_entries_empty_without_project(self):
        assert ps.project_digest_entries('', limit=10) == []
        assert ps.project_digest_entries('/tmp/nonexistent_proj_zzz', limit=10) == []

    def test_digest_empty_without_project(self):
        assert ps.build_project_digest('', limit=10) == ''
        assert ps.build_project_digest('/tmp/nonexistent_proj_zzz', limit=10) == ''

    def test_digest_relevance_gating_and_recency_floor(self):
        # Seed topically-distinct siblings in a dedicated project so BM25 can
        # discriminate. A query mentioning one topic must surface THAT sibling
        # first; an off-topic query must still return the recency floor.
        now = int(time.time() * 1000)
        tag = f'relgate-{now}'
        proj = f'/tmp/projsum_{tag}'

        def _mk(cid, title, summary, ts):
            settings = {'projectPath': proj,
                        'projectSummary': {'text': summary, 'generated_at': ts,
                                           'msg_count_at_gen': 8}}
            from lib.database._core_schema import CONVERSATIONS, upsert
            upsert(self.db, CONVERSATIONS, {
                'id': cid, 'user_id': 1, 'title': title,
                'messages': json.dumps([]), 'created_at': ts, 'updated_at': ts,
                'settings': json.dumps(settings), 'msg_count': 8,
                'search_text': title,
            }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                            'updated_at', 'settings', 'msg_count', 'search_text'],
               retry=True)
            self.ids.append(cid)
            return cid

        # ts ascending → c_recent is the most-recently-updated (recency floor).
        c_db = _mk(f'{tag}-db', 'PostgreSQL migration',
                   'Migrated the database layer from SQLite to PostgreSQL.', now + 1)
        c_css = _mk(f'{tag}-css', 'Sidebar styling',
                    'Tweaked the CSS for the conversation sidebar chips.', now + 2)
        c_recent = _mk(f'{tag}-recent', 'Latest unrelated work',
                       'Refactored the scheduler timer loop.', now + 3)

        # Relevant query → the matching sibling appears.
        entries = ps.project_digest_entries(
            proj, query='fix a bug in the postgresql database migration')
        ids = [e['id'] for e in entries]
        assert c_db in ids, 'relevant sibling must be surfaced'
        assert ids[0] == c_db, 'most-relevant sibling must rank first'

        # Off-topic query (nothing matches) → recency floor still returns
        # something, led by the most-recent sibling.
        off = ps.project_digest_entries(
            proj, query='quantum chromodynamics lattice gauge theory')
        off_ids = [e['id'] for e in off]
        assert off_ids, 'off-topic turn must NOT be empty (recency floor)'
        assert c_recent in off_ids, 'recency floor keeps the most-recent sibling'

        # No query → pure recency (back-compat), most-recent first.
        recency = ps.project_digest_entries(proj)
        assert [e['id'] for e in recency][:3] == [c_recent, c_css, c_db]

    def test_digest_header_advertises_tools_only_when_available(self):
        # When the conv-ref tools ARE registered, the header instructs the
        # model to call them. When they are NOT, the header must name no tool
        # the model can't call (mirrors the using-tools-section guardrail).
        with_tools = ps.build_project_digest(
            self.proj, current_conv_id=self.c_fresh, limit=10,
            conv_tools_available=True)
        without_tools = ps.build_project_digest(
            self.proj, current_conv_id=self.c_fresh, limit=10,
            conv_tools_available=False)
        assert 'list_conversations' in with_tools
        assert 'get_conversation' in with_tools
        assert 'list_conversations' not in without_tools
        assert 'get_conversation' not in without_tools
        # Both still surface the siblings + share the idempotency substring.
        assert self.c_grown in with_tools and self.c_grown in without_tools
        assert 'related conversation(s)' in with_tools
        assert 'related conversation(s)' in without_tools
