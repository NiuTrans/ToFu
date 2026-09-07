"""Tests for conversation search optimization.

Covers:
  - build_search_text() extraction logic (unit)
  - /api/v1/conversations/search endpoint (API integration)
  - tsvector + ILIKE two-phase search behavior
  - Snippet extraction correctness
  - Edge cases: empty query, special chars, unicode, no results

Run:
  pytest tests/test_conversation_search.py -m unit      # unit only
  pytest tests/test_conversation_search.py -m api       # API only
  pytest tests/test_conversation_search.py              # all
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import pytest

# Ensure project root on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conversations import build_search_text
from tests._seed import seed_conversation, wait_for_conversation_search

pytest_plugins = ('tests._chat_sidecar',)

_SEARCH_OWNER_USER_ID = 1


def _storage_client():
    from lib.storage import get_storage_client

    return get_storage_client(write=True)


def _search_items(resp):
    """Unwrap the charter#0 ``{ok, items}`` envelope.

    api-contract batch 20 (0aa0b6ff) migrated /api/v1/conversations/search
    from a bare array to the envelope — coordinated with the frontend
    (api.js:723 unwraps the same way). This suite pinned the old bare-array
    shape; direction-aligned to the envelope (same drift family as
    test_api_integration batch 9/11). Asserting the envelope here also kills
    the vacuous passers: iterating a dict yields KEYS, so ``for r in data``
    used to "pass" on 'ok'/'items' without ever seeing a result row.
    """
    data = resp.get_json()
    assert isinstance(data, dict) and data.get('ok') is True, (
        f'search envelope drifted: {data!r}')
    assert isinstance(data.get('items'), list)
    return data['items']


# ═══════════════════════════════════════════════════════════
#  Unit Tests: build_search_text
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBuildSearchText:
    """Unit tests for the search text extraction function."""

    def test_basic_messages(self):
        """Extracts content from user and assistant messages."""
        msgs = [
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = build_search_text(msgs)
        assert "Hello world" in result
        assert "Hi there!" in result

    def test_ignores_system_and_tool_roles(self):
        """Only user and assistant messages are indexed."""
        msgs = [
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "tool", "content": "Tool result data"},
            {"role": "user", "content": "keepme"},
        ]
        result = build_search_text(msgs)
        assert "helpful assistant" not in result
        assert "Tool result" not in result
        assert "keepme" in result

    def test_multipart_content(self):
        """Handles list-style multi-part content (text + images)."""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look at this image"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            },
        ]
        result = build_search_text(msgs)
        assert "Look at this image" in result

    def test_multipart_content_string_items(self):
        """Handles mixed string items in content list."""
        msgs = [
            {"role": "user", "content": ["Plain string item", {"text": "Dict item"}]},
        ]
        result = build_search_text(msgs)
        assert "Plain string item" in result
        assert "Dict item" in result

    def test_thinking_field_included(self):
        """Thinking/reasoning content is indexed for search."""
        msgs = [
            {
                "role": "assistant",
                "content": "The answer is 42",
                "thinking": "Let me think step by step about this problem",
            },
        ]
        result = build_search_text(msgs)
        assert "answer is 42" in result
        assert "step by step" in result

    def test_original_content_indexed(self):
        """Pre-auto-translation user text (originalContent) is indexed.

        Regression pin: when auto-translate-user is ON, a user's message is
        translated and `content` holds the English translation while the words
        the user actually typed live in `originalContent`. Both must be
        searchable, or the user can't find their own message by what they wrote.
        """
        msgs = [
            {
                "role": "user",
                "content": "when I dragged and dropped a compressed package to submit it",
                "originalContent": "我刚刚拖拽了一个压缩包提交到目录里",
            },
        ]
        result = build_search_text(msgs)
        assert "拖拽了一个压缩包提交" in result   # the original a user searches by
        assert "compressed package" in result   # translation stays searchable too

    def test_translated_content_indexed(self):
        """Assistant translatedContent stays indexed (sibling of the above)."""
        msgs = [
            {
                "role": "assistant",
                "content": "Here is the fix",
                "translatedContent": "这是修复方案",
            },
        ]
        result = build_search_text(msgs)
        assert "这是修复方案" in result
        assert "Here is the fix" in result

    def test_empty_messages(self):
        result = build_search_text([])
        assert result == ""

    def test_none_input(self):
        result = build_search_text(None)
        assert result == ""

    def test_json_string_input(self):
        """Accepts raw JSON string as input."""
        msgs = [{"role": "user", "content": "from json string"}]
        result = build_search_text(json.dumps(msgs))
        assert "from json string" in result

    def test_invalid_json_string(self):
        result = build_search_text("not valid json {{{")
        assert result == ""

    def test_empty_content(self):
        """Messages with empty or missing content produce no noise."""
        msgs = [
            {"role": "user", "content": ""},
            {"role": "assistant"},
            {"role": "user", "content": "actual content"},
        ]
        result = build_search_text(msgs)
        assert "actual content" in result
        # No extra blank lines from empty content
        assert result.strip() == "actual content"

    def test_non_dict_messages_skipped(self):
        """Gracefully skips non-dict items in messages list."""
        msgs = [
            "not a dict",
            42,
            None,
            {"role": "user", "content": "valid message"},
        ]
        result = build_search_text(msgs)
        assert "valid message" in result

    def test_unicode_content(self):
        """Chinese, emoji, and other unicode content preserved."""
        msgs = [
            {"role": "user", "content": "你好世界 🎉"},
            {"role": "assistant", "content": "こんにちは"},
        ]
        result = build_search_text(msgs)
        assert "你好世界" in result
        assert "🎉" in result
        assert "こんにちは" in result

    def test_newline_separation(self):
        """Messages are separated by newlines."""
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
        result = build_search_text(msgs)
        assert result == "first\nsecond"


# ═══════════════════════════════════════════════════════════
#  API Integration Tests: /api/v1/conversations/search
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestSearchEndpoint:
    """API tests for the conversation search endpoint.

    The search route is served by the storage Sidecar (``conversation.search``
    op — the legacy-engine scan was removed in the sidecar cutover), so these
    tests run against a real sidecar runtime. ``chat_sidecar`` is an EXPLICIT
    dependency of the seeding fixture (not a usefixtures mark) so the
    Sidecar authority is guaranteed active before semantic seeds run.
    """

    @pytest.fixture(autouse=True)
    def setup_test_conversations(self, chat_sidecar):
        """Create owner-scoped fixtures through semantic Sidecar operations."""
        now = int(time.time() * 1000)
        storage = _storage_client()
        self.conv_ids = []

        test_data = [
            {
                "id": f"search-test-alpha-{now}",
                "title": "Python Programming Discussion",
                "messages": [
                    {"role": "user", "content": "How do I use decorators in Python?", "timestamp": now},
                    {"role": "assistant", "content": "Decorators are a powerful Python feature that allows you to modify functions.", "timestamp": now + 1},
                ],
            },
            {
                "id": f"search-test-beta-{now}",
                "title": "Machine Learning Chat",
                "messages": [
                    {"role": "user", "content": "Explain gradient descent optimization", "timestamp": now},
                    {"role": "assistant", "content": "Gradient descent is an iterative algorithm used to minimize a loss function.", "timestamp": now + 1},
                ],
            },
            {
                "id": f"search-test-gamma-{now}",
                "title": "Database Query Help",
                "messages": [
                    {"role": "user", "content": "How to optimize PostgreSQL queries?", "timestamp": now},
                    {"role": "assistant", "content": "Use EXPLAIN ANALYZE, add indexes, and avoid sequential scans.", "timestamp": now + 1},
                ],
            },
            {
                "id": f"search-test-unicode-{now}",
                "title": "中文对话",
                "messages": [
                    {"role": "user", "content": "请解释搜索引擎的工作原理", "timestamp": now},
                    {"role": "assistant", "content": "搜索引擎通过爬虫抓取网页内容，然后建立索引。", "timestamp": now + 1},
                ],
            },
            {
                "id": f"search-test-unique-{now}",
                "title": "Unique Keyword Test",
                "messages": [
                    {"role": "user", "content": "Tell me about xylophone_zebra_quantum", "timestamp": now},
                    {"role": "assistant", "content": "That's a very unique combination of words!", "timestamp": now + 1},
                ],
            },
        ]

        for conv in test_data:
            seed_conversation(
                conv['id'],
                user_id=_SEARCH_OWNER_USER_ID,
                title=conv['title'],
                messages=conv['messages'],
                created_at=now,
                updated_at=now,
            )
            self.conv_ids.append(conv['id'])

        readiness_checks = (
            ('decorators', 'alpha'),
            ('gradient', 'beta'),
            ('PostgreSQL', 'gamma'),
            ('搜索引擎', 'unicode'),
            ('xylophone_zebra_quantum', 'unique'),
        )
        for query, id_fragment in readiness_checks:
            expected_id = next(
                conv_id for conv_id in self.conv_ids
                if id_fragment in conv_id)
            wait_for_conversation_search(
                query,
                user_id=_SEARCH_OWNER_USER_ID,
                expected_ids=(expected_id,),
                client=storage,
            )

        try:
            yield
        finally:
            for conv_id in self.conv_ids:
                storage.command('conversation.purge', {
                    'conv_id': conv_id,
                    'user_id': _SEARCH_OWNER_USER_ID,
                }, f'search-cleanup:{conv_id}:{uuid.uuid4().hex[:10]}')

    def test_search_finds_matching_content(self, flask_client):
        """Search returns conversations matching the query."""
        resp = flask_client.get("/api/v1/conversations/search?q=decorators")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert len(data) >= 1
        matched_ids = [r["id"] for r in data]
        alpha_id = [cid for cid in self.conv_ids if "alpha" in cid][0]
        assert alpha_id in matched_ids

    def test_search_returns_snippets(self, flask_client):
        """Search results include content snippets."""
        resp = flask_client.get("/api/v1/conversations/search?q=gradient")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert len(data) >= 1
        # At least one result should have a non-empty snippet
        snippets = [r.get("matchSnippet", "") for r in data]
        assert any(s for s in snippets), f"No snippets found in results: {data}"

    def test_search_snippet_contains_query(self, flask_client):
        """Snippet should contain (or be near) the search query."""
        resp = flask_client.get("/api/v1/conversations/search?q=gradient")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert len(data) >= 1
        # The snippet should contain the query term (case-insensitive)
        for r in data:
            snippet = r.get("matchSnippet", "").lower()
            if snippet:
                assert "gradient" in snippet, f"Snippet doesn't contain query: {snippet}"

    def test_search_no_results(self, flask_client):
        """Search with non-matching query returns empty list."""
        resp = flask_client.get("/api/v1/conversations/search?q=zzznonexistentxxx999")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert data == []

    def test_search_empty_query_rejected(self, flask_client):
        """Empty or too-short queries return empty results."""
        for q in ["", " ", "a"]:
            resp = flask_client.get(f"/api/v1/conversations/search?q={q}")
            assert resp.status_code == 200
            assert _search_items(resp) == []

    def test_search_no_query_param(self, flask_client):
        """Missing q parameter returns empty results."""
        resp = flask_client.get("/api/v1/conversations/search")
        assert resp.status_code == 200
        assert _search_items(resp) == []

    def test_search_unicode_chinese(self, flask_client):
        """Chinese text search works correctly."""
        resp = flask_client.get("/api/v1/conversations/search?q=搜索引擎")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert len(data) >= 1
        unicode_id = [cid for cid in self.conv_ids if "unicode" in cid][0]
        matched_ids = [r["id"] for r in data]
        assert unicode_id in matched_ids

    def test_search_unique_term(self, flask_client):
        """Unique/rare terms are found correctly."""
        resp = flask_client.get("/api/v1/conversations/search?q=xylophone_zebra_quantum")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert len(data) >= 1
        unique_id = [cid for cid in self.conv_ids if "unique" in cid][0]
        matched_ids = [r["id"] for r in data]
        assert unique_id in matched_ids

    def test_search_case_insensitive(self, flask_client):
        """Search is case-insensitive."""
        resp_lower = flask_client.get("/api/v1/conversations/search?q=python")
        resp_upper = flask_client.get("/api/v1/conversations/search?q=PYTHON")
        assert resp_lower.status_code == 200
        assert resp_upper.status_code == 200
        ids_lower = {r["id"] for r in _search_items(resp_lower)}
        ids_upper = {r["id"] for r in _search_items(resp_upper)}
        # Both should find the same test conversation
        alpha_id = [cid for cid in self.conv_ids if "alpha" in cid][0]
        assert alpha_id in ids_lower
        assert alpha_id in ids_upper

    def test_search_result_shape(self, flask_client):
        """Each search result has the expected fields."""
        resp = flask_client.get("/api/v1/conversations/search?q=python")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert len(data) >= 1
        for result in data:
            assert "id" in result
            assert "matchField" in result
            assert "matchSnippet" in result
            assert "matchRole" in result
            assert result["matchField"] == "content"

    def test_search_special_characters(self, flask_client):
        """Search handles SQL special characters safely (no injection)."""
        # These should not crash the endpoint
        special_queries = [
            "test%drop",
            "test_table",
            "it's a test",
            "test'; DROP TABLE--",
            "test\\ninjection",
            "test (parentheses)",
        ]
        for q in special_queries:
            resp = flask_client.get("/api/v1/conversations/search", query_string={"q": q})
            assert resp.status_code == 200, f"Crashed on query: {q}"
            data = _search_items(resp)
            assert isinstance(data, list), f"Non-list response for query: {q}"

    def test_search_max_results_capped(self, flask_client):
        """Search returns at most 50 results."""
        # "the" should match many conversations
        resp = flask_client.get("/api/v1/conversations/search?q=the")
        assert resp.status_code == 200
        data = _search_items(resp)
        assert len(data) <= 50

    def test_search_performance(self, flask_client):
        """Search should complete in reasonable time.

        Warm the path once (the first request pays one-time costs: query
        compilation, FTS index warm-up, connection setup) then assert on a
        steady-state request so the threshold isn't flaky under CI load.
        """
        flask_client.get("/api/v1/conversations/search?q=python")  # warm-up
        t0 = time.monotonic()
        resp = flask_client.get("/api/v1/conversations/search?q=python")
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        assert elapsed < 1.0, f"Search took {elapsed:.3f}s, expected <1.0s"

    def test_search_does_not_return_messages(self, flask_client):
        """Search results should NOT include full messages (performance)."""
        resp = flask_client.get("/api/v1/conversations/search?q=decorators")
        assert resp.status_code == 200
        data = _search_items(resp)
        for result in data:
            assert "messages" not in result, "Search should not return full messages"

    def test_search_after_update(self, flask_client):
        """Updating a conversation's messages updates the search index."""
        now = int(time.time() * 1000)
        conv_id = f"search-test-update-{now}"
        self.conv_ids.append(conv_id)

        seed_conversation(
            conv_id,
            user_id=_SEARCH_OWNER_USER_ID,
            title='Update Test',
            messages=[{
                'role': 'user',
                'content': 'original_platypus_content',
                'timestamp': now,
            }],
            created_at=now,
            updated_at=now,
        )
        wait_for_conversation_search(
            'original_platypus_content',
            user_id=_SEARCH_OWNER_USER_ID,
            expected_ids=(conv_id,),
        )

        # Should find original content
        resp = flask_client.get("/api/v1/conversations/search?q=original_platypus_content")
        assert resp.status_code == 200
        ids = [r["id"] for r in _search_items(resp)]
        assert conv_id in ids

        # Use the canonical settled-Turn CAS edit.  Search projection refresh
        # is part of the same Sidecar transaction.
        client = _storage_client()
        turns = client.query('turn.list', {
            'conversation_id': conv_id,
            'user_id': _SEARCH_OWNER_USER_ID,
        })
        assert len(turns) == 1
        original_turn = turns[0]
        client.command('turn.projection.update', {
            'conversation_id': conv_id,
            'user_id': _SEARCH_OWNER_USER_ID,
            'turn_id': original_turn['turnId'],
            'expected_projection_revision': original_turn[
                'projectionRevision'],
            'projection': {
                'content': 'updated_narwhal_content',
                'timestamp': now + 1,
            },
        }, f'search-update:{conv_id}:{uuid.uuid4().hex[:10]}')
        wait_for_conversation_search(
            'updated_narwhal_content',
            user_id=_SEARCH_OWNER_USER_ID,
            expected_ids=(conv_id,),
            client=client,
        )

        # Should find new content
        resp = flask_client.get("/api/v1/conversations/search?q=updated_narwhal_content")
        assert resp.status_code == 200
        ids = [r["id"] for r in _search_items(resp)]
        assert conv_id in ids

        # Old content should no longer match
        resp = flask_client.get("/api/v1/conversations/search?q=original_platypus_content")
        assert resp.status_code == 200
        ids = [r["id"] for r in _search_items(resp)]
        assert conv_id not in ids

    def test_storage_timeout_is_typed_retryable_503(
            self, flask_client, monkeypatch):
        """A sidecar wedge is never laundered into an empty successful hit
        list (or an opaque 500)."""
        from lib.storage.errors import StorageError
        import lib.storage

        def fail_client(*_args, **_kwargs):
            raise StorageError(
                'database_timeout', 'search deadline exceeded',
                retryable=True, retry_after_ms=125,
                operation_id='search-timeout-test')

        monkeypatch.setattr(lib.storage, 'get_storage_client', fail_client)
        response = flask_client.get(
            '/api/v1/conversations/search?q=typed-timeout')

        assert response.status_code == 503
        assert response.headers['Retry-After'] == '1'
        body = response.get_json()
        assert body['error']['kind'] == 'server_busy'
        assert body['storageCode'] == 'database_timeout'
        assert body['operationId'] == 'search-timeout-test'

    def test_search_threads_request_owner_to_storage(
            self, flask_client, monkeypatch):
        """The route never hardcodes the personal-server owner in core I/O."""
        import lib.storage
        import routes.conversations_search as search_routes

        observed = []

        class Client:
            def query(self, operation, payload):
                observed.append((operation, dict(payload)))
                return []

        monkeypatch.setattr(lib.storage, 'get_storage_client', lambda: Client())
        monkeypatch.setattr(search_routes, '_request_user_id', lambda: 42)

        response = flask_client.get(
            '/api/v1/conversations/search?q=owner-scope')

        assert response.status_code == 200
        assert observed == [('conversation.search', {
            'user_id': 42,
            'query': 'owner-scope',
            'limit': 50,
            'snippet_radius': 40,
        })]

    def test_search_after_delete(self, flask_client):
        """Deleted conversations don't appear in search results."""
        now = int(time.time() * 1000)
        conv_id = f"search-test-delete-{now}"
        self.conv_ids.append(conv_id)

        seed_conversation(
            conv_id,
            user_id=_SEARCH_OWNER_USER_ID,
            title='Delete Test',
            messages=[{
                'role': 'user',
                'content': 'ephemeral_flamingo_search',
                'timestamp': now,
            }],
            created_at=now,
            updated_at=now,
        )
        wait_for_conversation_search(
            'ephemeral_flamingo_search',
            user_id=_SEARCH_OWNER_USER_ID,
            expected_ids=(conv_id,),
        )

        # Verify it's findable
        resp = flask_client.get("/api/v1/conversations/search?q=ephemeral_flamingo_search")
        assert resp.status_code == 200
        assert any(r["id"] == conv_id for r in _search_items(resp))

        _storage_client().command('conversation.delete', {
            'conv_id': conv_id,
            'user_id': _SEARCH_OWNER_USER_ID,
        }, f'search-delete:{conv_id}:{uuid.uuid4().hex[:10]}')
        wait_for_conversation_search(
            'ephemeral_flamingo_search',
            user_id=_SEARCH_OWNER_USER_ID,
            absent_ids=(conv_id,),
        )

        # Should no longer appear
        resp = flask_client.get("/api/v1/conversations/search?q=ephemeral_flamingo_search")
        assert resp.status_code == 200
        assert not any(r["id"] == conv_id for r in _search_items(resp))

    def test_slow_search_threshold_log(self):
        """The timing helper logs WARNING above the threshold, DEBUG below —
        so a regression in the index path is visible in app.log."""
        import logging

        from routes import conversations_search as cs

        records = []

        class _Cap(logging.Handler):
            def emit(self, rec):
                records.append((rec.levelno, rec.getMessage()))

        h = _Cap()
        cs.logger.addHandler(h)
        old_level = cs.logger.level
        cs.logger.setLevel(logging.DEBUG)
        try:
            cs._log_search_timing('q', 3, cs._SLOW_SEARCH_THRESHOLD_S + 0.5)
            cs._log_search_timing('q', 3, 0.001)
        finally:
            cs.logger.removeHandler(h)
            cs.logger.setLevel(old_level)

        levels = [lvl for lvl, _ in records]
        assert logging.WARNING in levels, 'slow search should log WARNING'
        assert logging.DEBUG in levels, 'fast search should log DEBUG'
        slow_msg = next(m for lvl, m in records if lvl == logging.WARNING)
        assert 'SLOW' in slow_msg

    def test_search_substring_match(self, flask_client):
        """ILIKE fallback finds substring matches that tsvector misses."""
        now = int(time.time() * 1000)
        conv_id = f"search-test-substr-{now}"
        self.conv_ids.append(conv_id)

        seed_conversation(
            conv_id,
            user_id=_SEARCH_OWNER_USER_ID,
            title='Substring Test',
            messages=[{
                'role': 'user',
                'content': 'The superbacktesting framework is great',
                'timestamp': now,
            }],
            created_at=now,
            updated_at=now,
        )
        wait_for_conversation_search(
            'superbacktest',
            user_id=_SEARCH_OWNER_USER_ID,
            expected_ids=(conv_id,),
        )

        # Search for a substring that appears mid-word
        # tsvector won't match "backtest" inside "superbacktesting", but ILIKE will
        resp = flask_client.get("/api/v1/conversations/search?q=superbacktest")
        assert resp.status_code == 200
        data = _search_items(resp)
        ids = [r["id"] for r in data]
        assert conv_id in ids, f"Substring match not found. Results: {data}"
