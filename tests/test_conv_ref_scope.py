"""tests/test_conv_ref_scope.py — project-scoped conversation listing.

Pins Layer 1 of the cross-conversation awareness work: ``list_conversations``
can scope results to OTHER conversations of the same project (via
``settings.projectPath``), matches a keyword against message CONTENT (not just
the title), and excludes the current conversation.

Seeds immutable fixtures through the semantic import operation.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conv_ref import list_conversations

pytest_plugins = ('tests._chat_sidecar',)


@pytest.mark.api
class TestListConversationsScope:
    @pytest.fixture(autouse=True)
    def seed(self, flask_client, chat_sidecar):
        from tests._seed import (
            seed_conversation,
            wait_for_conversation_search,
        )
        now = int(time.time() * 1000)
        tag = f'{now}'
        self.proj_a = f'/tmp/proj_a_{tag}'
        self.proj_b = f'/tmp/proj_b_{tag}'
        self.ids = {
            'a1': f'cvscope-a1-{tag}',
            'a2': f'cvscope-a2-{tag}',
            'b1': f'cvscope-b1-{tag}',
        }
        def _seed(cid, title, project, body, ts):
            seed_conversation(
                cid, title=title, created_at=ts, updated_at=ts,
                settings={'projectPath': project},
                messages=[
                    {'role': 'user', 'content': body},
                    {'role': 'assistant', 'content': 'acknowledged'},
                ])

        # a1: project A, body mentions the rare term; a2: project A, no term;
        # b1: project B, body mentions the rare term (must be excluded by scope).
        self._term = f'zqxprojscope{tag}'
        _seed(self.ids['a1'], 'Alpha One', self.proj_a,
              f'we discussed {self._term} extensively', now + 3)
        _seed(self.ids['a2'], 'Alpha Two', self.proj_a,
              'unrelated chatter only', now + 2)
        _seed(self.ids['b1'], 'Beta One', self.proj_b,
              f'also about {self._term} but other project', now + 1)
        wait_for_conversation_search(
            self._term,
            expected_ids=(self.ids['a1'], self.ids['b1']),
        )
        yield

    def test_project_scope_excludes_other_projects(self):
        out = list_conversations(
            scope='project', project_path=self.proj_a, limit=50, user_id=1)
        assert self.ids['a1'] in out
        assert self.ids['a2'] in out
        assert self.ids['b1'] not in out  # different project — excluded

    def test_content_keyword_matches_body_not_just_title(self):
        # The term lives only in search_text bodies, never in a title.
        out = list_conversations(
            keyword=self._term, scope='all', limit=50, user_id=1)
        assert self.ids['a1'] in out
        assert self.ids['b1'] in out
        assert self.ids['a2'] not in out  # body has no term

    def test_project_scope_plus_keyword(self):
        out = list_conversations(keyword=self._term, scope='project',
                                 project_path=self.proj_a, limit=50, user_id=1)
        assert self.ids['a1'] in out      # project A + term
        assert self.ids['a2'] not in out  # project A but no term
        assert self.ids['b1'] not in out  # has term but wrong project

    def test_current_conv_excluded(self):
        out = list_conversations(scope='project', project_path=self.proj_a,
                                 current_conv_id=self.ids['a1'], limit=50,
                                 user_id=1)
        assert self.ids['a1'] not in out
        assert self.ids['a2'] in out

    def test_auto_scope_falls_back_to_all_without_project(self):
        # No project_path → auto degrades to 'all'; the term still finds both.
        out = list_conversations(
            keyword=self._term, scope='auto', limit=50, user_id=1)
        assert self.ids['a1'] in out
        assert self.ids['b1'] in out
