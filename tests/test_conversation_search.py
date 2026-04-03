"""Tests for conversation search optimization.

Covers:
  - build_search_text() extraction logic (unit)
  - /api/conversations/search endpoint (API integration)
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

import pytest

# Ensure project root on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.conversations import build_search_text

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
#  API Integration Tests: /api/conversations/search
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestSearchEndpoint:
    """API tests for the conversation search endpoint."""

    @pytest.fixture(autouse=True)
    def setup_test_conversations(self, flask_client):
        """Create a set of test conversations for search tests."""
        now = int(time.time() * 1000)
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
            resp = flask_client.put(
                f"/api/conversations/{conv['id']}",
                json={
                    "title": conv["title"],
                    "messages": conv["messages"],
                    "createdAt": now,
                    "updatedAt": now,
                },
            )
            assert resp.status_code == 200, f"Failed to save conv {conv['id']}: {resp.data}"
            self.conv_ids.append(conv["id"])

        yield

        # Cleanup
        for conv_id in self.conv_ids:
            flask_client.delete(f"/api/conversations/{conv_id}")

    def test_search_finds_matching_content(self, flask_client):
        """Search returns conversations matching the query."""
        resp = flask_client.get("/api/conversations/search?q=decorators")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        matched_ids = [r["id"] for r in data]
        alpha_id = [cid for cid in self.conv_ids if "alpha" in cid][0]
        assert alpha_id in matched_ids

    def test_search_returns_snippets(self, flask_client):
        """Search results include content snippets."""
        resp = flask_client.get("/api/conversations/search?q=gradient")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        # At least one result should have a non-empty snippet
        snippets = [r.get("matchSnippet", "") for r in data]
        assert any(s for s in snippets), f"No snippets found in results: {data}"

    def test_search_snippet_contains_query(self, flask_client):
        """Snippet should contain (or be near) the search query."""
        resp = flask_client.get("/api/conversations/search?q=gradient")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        # The snippet should contain the query term (case-insensitive)
        for r in data:
            snippet = r.get("matchSnippet", "").lower()
            if snippet:
                assert "gradient" in snippet, f"Snippet doesn't contain query: {snippet}"

    def test_search_no_results(self, flask_client):
        """Search with non-matching query returns empty list."""
        resp = flask_client.get("/api/conversations/search?q=zzznonexistentxxx999")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []

    def test_search_empty_query_rejected(self, flask_client):
        """Empty or too-short queries return empty results."""
        for q in ["", " ", "a"]:
            resp = flask_client.get(f"/api/conversations/search?q={q}")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_search_no_query_param(self, flask_client):
        """Missing q parameter returns empty results."""
        resp = flask_client.get("/api/conversations/search")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_search_unicode_chinese(self, flask_client):
        """Chinese text search works correctly."""
        resp = flask_client.get("/api/conversations/search?q=搜索引擎")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        unicode_id = [cid for cid in self.conv_ids if "unicode" in cid][0]
        matched_ids = [r["id"] for r in data]
        assert unicode_id in matched_ids

    def test_search_unique_term(self, flask_client):
        """Unique/rare terms are found correctly."""
        resp = flask_client.get("/api/conversations/search?q=xylophone_zebra_quantum")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) >= 1
        unique_id = [cid for cid in self.conv_ids if "unique" in cid][0]
        matched_ids = [r["id"] for r in data]
        assert unique_id in matched_ids

    def test_search_case_insensitive(self, flask_client):
        """Search is case-insensitive."""
        resp_lower = flask_client.get("/api/conversations/search?q=python")
        resp_upper = flask_client.get("/api/conversations/search?q=PYTHON")
        assert resp_lower.status_code == 200
        assert resp_upper.status_code == 200
        ids_lower = {r["id"] for r in resp_lower.get_json()}
        ids_upper = {r["id"] for r in resp_upper.get_json()}
        # Both should find the same test conversation
        alpha_id = [cid for cid in self.conv_ids if "alpha" in cid][0]
        assert alpha_id in ids_lower
        assert alpha_id in ids_upper

    def test_search_result_shape(self, flask_client):
        """Each search result has the expected fields."""
        resp = flask_client.get("/api/conversations/search?q=python")
        assert resp.status_code == 200
        data = resp.get_json()
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
            resp = flask_client.get("/api/conversations/search", query_string={"q": q})
            assert resp.status_code == 200, f"Crashed on query: {q}"
            data = resp.get_json()
            assert isinstance(data, list), f"Non-list response for query: {q}"

    def test_search_max_results_capped(self, flask_client):
        """Search returns at most 50 results."""
        # "the" should match many conversations
        resp = flask_client.get("/api/conversations/search?q=the")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) <= 50

    def test_search_performance(self, flask_client):
        """Search should complete in reasonable time (<500ms)."""
        t0 = time.monotonic()
        resp = flask_client.get("/api/conversations/search?q=python")
        elapsed = time.monotonic() - t0
        assert resp.status_code == 200
        assert elapsed < 0.5, f"Search took {elapsed:.3f}s, expected <0.5s"

    def test_search_does_not_return_messages(self, flask_client):
        """Search results should NOT include full messages (performance)."""
        resp = flask_client.get("/api/conversations/search?q=decorators")
        assert resp.status_code == 200
        data = resp.get_json()
        for result in data:
            assert "messages" not in result, "Search should not return full messages"

    def test_search_after_update(self, flask_client):
        """Updating a conversation's messages updates the search index."""
        now = int(time.time() * 1000)
        conv_id = f"search-test-update-{now}"
        self.conv_ids.append(conv_id)

        # Create with original content
        flask_client.put(f"/api/conversations/{conv_id}", json={
            "title": "Update Test",
            "messages": [{"role": "user", "content": "original_platypus_content", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now,
        })

        # Should find original content
        resp = flask_client.get("/api/conversations/search?q=original_platypus_content")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.get_json()]
        assert conv_id in ids

        # Update with new content
        flask_client.put(f"/api/conversations/{conv_id}", json={
            "title": "Update Test",
            "messages": [{"role": "user", "content": "updated_narwhal_content", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now + 1,
        })

        # Should find new content
        resp = flask_client.get("/api/conversations/search?q=updated_narwhal_content")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.get_json()]
        assert conv_id in ids

        # Old content should no longer match
        resp = flask_client.get("/api/conversations/search?q=original_platypus_content")
        assert resp.status_code == 200
        ids = [r["id"] for r in resp.get_json()]
        assert conv_id not in ids

    def test_search_after_delete(self, flask_client):
        """Deleted conversations don't appear in search results."""
        now = int(time.time() * 1000)
        conv_id = f"search-test-delete-{now}"

        # Create
        flask_client.put(f"/api/conversations/{conv_id}", json={
            "title": "Delete Test",
            "messages": [{"role": "user", "content": "ephemeral_flamingo_search", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now,
        })

        # Verify it's findable
        resp = flask_client.get("/api/conversations/search?q=ephemeral_flamingo_search")
        assert resp.status_code == 200
        assert any(r["id"] == conv_id for r in resp.get_json())

        # Delete
        flask_client.delete(f"/api/conversations/{conv_id}")

        # Should no longer appear
        resp = flask_client.get("/api/conversations/search?q=ephemeral_flamingo_search")
        assert resp.status_code == 200
        assert not any(r["id"] == conv_id for r in resp.get_json())

    def test_search_substring_match(self, flask_client):
        """ILIKE fallback finds substring matches that tsvector misses."""
        now = int(time.time() * 1000)
        conv_id = f"search-test-substr-{now}"
        self.conv_ids.append(conv_id)

        # Create conv with a compound word
        flask_client.put(f"/api/conversations/{conv_id}", json={
            "title": "Substring Test",
            "messages": [{"role": "user", "content": "The superbacktesting framework is great", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now,
        })

        # Search for a substring that appears mid-word
        # tsvector won't match "backtest" inside "superbacktesting", but ILIKE will
        resp = flask_client.get("/api/conversations/search?q=superbacktest")
        assert resp.status_code == 200
        data = resp.get_json()
        ids = [r["id"] for r in data]
        assert conv_id in ids, f"Substring match not found. Results: {data}"
