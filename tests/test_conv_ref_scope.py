"""tests/test_conv_ref_scope.py — project-scoped conversation listing.

Pins Layer 1 of the cross-conversation awareness work: ``list_conversations``
can scope results to OTHER conversations of the same project (via
``settings.projectPath``), matches a keyword against message CONTENT (not just
the title), and excludes the current conversation.

Seeds rows directly via the shared ``upsert`` path (mirrors
test_conversation_search.py) so it runs on whichever backend the test DB uses.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conv_ref import list_conversations


@pytest.mark.api
class TestListConversationsScope:
    @pytest.fixture(autouse=True)
    def seed(self, flask_client):
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database._core_schema import CONVERSATIONS, upsert

        now = int(time.time() * 1000)
        tag = f'{now}'
        self.proj_a = f'/tmp/proj_a_{tag}'
        self.proj_b = f'/tmp/proj_b_{tag}'
        self.ids = {
            'a1': f'cvscope-a1-{tag}',
            'a2': f'cvscope-a2-{tag}',
            'b1': f'cvscope-b1-{tag}',
        }
        db = get_thread_db(DOMAIN_CHAT)
        import json as _json

        def _seed(cid, title, project, body, ts):
            upsert(db, CONVERSATIONS, {
                'id': cid, 'user_id': 1, 'title': title,
                'messages': '[]', 'created_at': ts, 'updated_at': ts,
                'settings': _json.dumps({'projectPath': project}),
                'msg_count': 2, 'search_text': body,
            }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                            'updated_at', 'settings', 'msg_count', 'search_text'],
               retry=True)

        # a1: project A, body mentions the rare term; a2: project A, no term;
        # b1: project B, body mentions the rare term (must be excluded by scope).
        self._term = f'zqxprojscope{tag}'
        _seed(self.ids['a1'], 'Alpha One', self.proj_a,
              f'we discussed {self._term} extensively', now + 3)
        _seed(self.ids['a2'], 'Alpha Two', self.proj_a,
              'unrelated chatter only', now + 2)
        _seed(self.ids['b1'], 'Beta One', self.proj_b,
              f'also about {self._term} but other project', now + 1)
        yield
        for cid in self.ids.values():
            db.execute('DELETE FROM conversations WHERE id=?', (cid,))
        db.commit()

    def test_project_scope_excludes_other_projects(self):
        out = list_conversations(scope='project', project_path=self.proj_a, limit=50)
        assert self.ids['a1'] in out
        assert self.ids['a2'] in out
        assert self.ids['b1'] not in out  # different project — excluded

    def test_content_keyword_matches_body_not_just_title(self):
        # The term lives only in search_text bodies, never in a title.
        out = list_conversations(keyword=self._term, scope='all', limit=50)
        assert self.ids['a1'] in out
        assert self.ids['b1'] in out
        assert self.ids['a2'] not in out  # body has no term

    def test_project_scope_plus_keyword(self):
        out = list_conversations(keyword=self._term, scope='project',
                                 project_path=self.proj_a, limit=50)
        assert self.ids['a1'] in out      # project A + term
        assert self.ids['a2'] not in out  # project A but no term
        assert self.ids['b1'] not in out  # has term but wrong project

    def test_current_conv_excluded(self):
        out = list_conversations(scope='project', project_path=self.proj_a,
                                 current_conv_id=self.ids['a1'], limit=50)
        assert self.ids['a1'] not in out
        assert self.ids['a2'] in out

    def test_auto_scope_falls_back_to_all_without_project(self):
        # No project_path → auto degrades to 'all'; the term still finds both.
        out = list_conversations(keyword=self._term, scope='auto', limit=50)
        assert self.ids['a1'] in out
        assert self.ids['b1'] in out
