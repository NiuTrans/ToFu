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

import pytest

# Ensure project root on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.conversations import build_search_text

from routes.conversations_search import _head_cap_sql, _snippet_projection_sql


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
                f"/api/v1/conversations/{conv['id']}",
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
            flask_client.delete(f"/api/v1/conversations/{conv_id}")

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

        # Create with original content
        flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Update Test",
            "messages": [{"role": "user", "content": "original_platypus_content", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now,
        })

        # Should find original content
        resp = flask_client.get("/api/v1/conversations/search?q=original_platypus_content")
        assert resp.status_code == 200
        ids = [r["id"] for r in _search_items(resp)]
        assert conv_id in ids

        # Update with new content
        flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Update Test",
            "messages": [{"role": "user", "content": "updated_narwhal_content", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now + 1,
        })

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

    def test_search_after_delete(self, flask_client):
        """Deleted conversations don't appear in search results."""
        now = int(time.time() * 1000)
        conv_id = f"search-test-delete-{now}"

        # Create
        flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Delete Test",
            "messages": [{"role": "user", "content": "ephemeral_flamingo_search", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now,
        })

        # Verify it's findable
        resp = flask_client.get("/api/v1/conversations/search?q=ephemeral_flamingo_search")
        assert resp.status_code == 200
        assert any(r["id"] == conv_id for r in _search_items(resp))

        # Delete
        flask_client.delete(f"/api/v1/conversations/{conv_id}")

        # Should no longer appear
        resp = flask_client.get("/api/v1/conversations/search?q=ephemeral_flamingo_search")
        assert resp.status_code == 200
        assert not any(r["id"] == conv_id for r in _search_items(resp))

    def test_search_pg_phase1_uses_tsvector_index(self, flask_client):
        """On PG, a whole-word query must be served by the index-backed
        Phase-1 (search_tsv), NOT a sequential scan. This pins the fix for
        the ~790ms full Seq Scan that PG searches previously paid on every
        keystroke. Skips on SQLite (different Phase-1 path)."""
        from lib.database import _BACKEND
        if _BACKEND != 'pg':
            pytest.skip('PG-specific index path')

        import asyncio

        from lib.database import DOMAIN_CHAT, async_transaction

        # The endpoint builds 'word:*' prefix tsquery terms; mirror that and
        # assert the GIN index (idx_conv_search_tsv) is USABLE for Phase-1.
        #
        # We force ``enable_seqscan = off`` for the EXPLAIN: on the tiny test
        # fixture (~a dozen rows) PG's cost-based planner correctly prefers a
        # Seq Scan — an index probe is genuinely more expensive than scanning
        # a handful of rows — so asserting the plan *chose* the index is
        # non-deterministic and table-size-dependent. The durable property
        # this test pins is that the index EXISTS and APPLIES to the query
        # (the regression target is the index being dropped or the column
        # losing its GIN index), which surfaces deterministically once seqscan
        # is disabled. ``SET LOCAL`` inside async_transaction scopes the
        # override to this txn and rolls it back, so it never leaks onto the
        # pooled connection and skews unrelated queries.
        async def _plan():
            async with async_transaction(domain=DOMAIN_CHAT) as conn:
                await conn.execute('SET LOCAL enable_seqscan = off')
                rows = await conn.fetchall(
                    "EXPLAIN SELECT id FROM conversations "
                    "WHERE user_id=? AND search_tsv @@ to_tsquery('simple', ?) "
                    "ORDER BY updated_at DESC LIMIT 50",
                    (1, 'gradient:*'))
            return rows

        rows = asyncio.run(_plan())
        plan = '\n'.join(str(r[0]) for r in rows)
        assert 'idx_conv_search_tsv' in plan or 'Bitmap Index Scan' in plan, (
            f'Phase-1 PG search cannot use the tsvector GIN index '
            f'(index missing or dropped?):\n{plan}')


    def test_search_pg_fallback_uses_head_trgm_index(self, flask_client):
        """On PG, the Phase-2 substring fallback (`lower(left(search_text,
        10000)) LIKE ?`) must be served by the expression trgm index
        ``idx_conv_search_head_trgm``, NOT a full Seq Scan that detoasts every
        row. This pins the fix for the ~1.2s fallback scan. The index
        expression MUST match the query predicate (same lower(left(...,10000))
        shape) or the planner won't use it.

        Like the tsvector test above, we force ``enable_seqscan = off`` so the
        assertion is about the index being USABLE/APPLICABLE (the regression
        target: index dropped, or the 10000 cap drifting out of sync with the
        SQL in conversations_search.py), not about the cost-based planner's
        choice on the tiny test fixture."""
        from lib.database import _BACKEND
        if _BACKEND != 'pg':
            pytest.skip('PG-specific index path')

        import asyncio

        from lib.database import DOMAIN_CHAT, async_transaction

        async def _plan():
            async with async_transaction(domain=DOMAIN_CHAT) as conn:
                await conn.execute('SET LOCAL enable_seqscan = off')
                rows = await conn.fetchall(
                    "EXPLAIN SELECT id FROM conversations "
                    "WHERE user_id=? AND lower(left(search_text, 10000)) LIKE ? "
                    "ORDER BY updated_at DESC LIMIT 50",
                    (1, '%gradient%'))
            return rows

        rows = asyncio.run(_plan())
        plan = '\n'.join(str(r[0]) for r in rows)
        assert 'idx_conv_search_head_trgm' in plan or 'Bitmap Index Scan' in plan, (
            f'Phase-2 PG search fallback cannot use the head-trgm GIN index '
            f'(index missing, or left(...) cap out of sync with the SQL?):\n{plan}')

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

        # Create conv with a compound word
        flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Substring Test",
            "messages": [{"role": "user", "content": "The superbacktesting framework is great", "timestamp": now}],
            "createdAt": now,
            "updatedAt": now,
        })

        # Search for a substring that appears mid-word
        # tsvector won't match "backtest" inside "superbacktesting", but ILIKE will
        resp = flask_client.get("/api/v1/conversations/search?q=superbacktest")
        assert resp.status_code == 200
        data = _search_items(resp)
        ids = [r["id"] for r in data]
        assert conv_id in ids, f"Substring match not found. Results: {data}"



@pytest.mark.api
class TestUpsertRetryCrossConnectionVisibility:
    """Regression pin for the retry=True durability bug (2026-06-10).

    `upsert(..., retry=True)` routes through db_execute_with_retry. A bug where
    the retry branch forwarded the default `commit=False` left the write
    UNCOMMITTED: visible to the writing thread-local sync connection (same txn)
    but INVISIBLE to any OTHER connection — notably the async read pool
    (lib/database/aio.async_fetchall borrows a separate pooled connection).
    This surfaced as conversation-search returning 0 hits for freshly-seeded
    rows. The fix makes retry=True always commit. This test fails if that ever
    regresses: it seeds via upsert(retry=True) on the sync connection and reads
    back via async_fetchall on a DIFFERENT connection.
    """

    def test_retry_upsert_is_visible_to_async_pool(self, flask_client):
        import asyncio

        from lib.database import DOMAIN_CHAT, async_fetchall, get_thread_db
        from lib.database._core_schema import CONVERSATIONS, upsert

        conv_id = f'__retry_visibility_probe_{int(time.time()*1000)}__'
        marker = 'zzqretryvisibilityprobe'
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        try:
            # Seed via the exact path the converted call-sites use.
            upsert(db, CONVERSATIONS, {
                'id': conv_id, 'user_id': 1, 'title': 'retry-probe',
                'messages': '[]', 'created_at': now, 'updated_at': now,
                'settings': '{}', 'msg_count': 1,
                'search_text': f'hello {marker} world',
            }, insert_cols=['id', 'user_id', 'title', 'messages', 'created_at',
                            'updated_at', 'settings', 'msg_count', 'search_text'],
               retry=True)

            # Read back on a SEPARATE (async-pool) connection. If retry=True did
            # not commit, this returns nothing even though the sync conn sees it.
            async def _read():
                return await async_fetchall(
                    'SELECT id FROM conversations WHERE user_id=? '
                    'AND lower(search_text) LIKE ? LIMIT 50',
                    (1, f'%{marker}%'), domain=DOMAIN_CHAT)

            rows = asyncio.run(_read())
            assert any(r['id'] == conv_id for r in rows), (
                'upsert(retry=True) write not visible to async_fetchall — '
                'retry path is not committing (durability regression)')
        finally:
            db.execute('DELETE FROM conversations WHERE id=?', (conv_id,))
            db.commit()



# ═══════════════════════════════════════════════════════════
#  Cross-backend Phase-2 head-cap (SQLite has no left())
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPhase2HeadCapCrossBackend:
    """Regression pin for the ``no such function: left`` bug.

    The Phase-2 substring fallback caps the scan to the first 10000 chars of
    ``search_text``. It previously spelled that cap ``left(search_text, 10000)``
    unconditionally — but ``left()`` is a PostgreSQL/MySQL builtin that SQLite
    does NOT have. On every SQLite-fallback deployment the fallback query raised
    ``OperationalError: no such function: left``, which the ``except`` swallowed
    at WARNING → the substring search silently returned nothing (degraded search
    that no test caught, but which fired dozens of times in logs/error.log on
    the async DB threads).

    The fix (routes/conversations_search.py::_head_cap_sql) makes the head-cap
    backend-aware: ``left(...)`` on PG (so it still hits the expression index
    ``idx_conv_search_head_trgm``) and portable ``substr(..., 1, 10000)`` on
    SQLite.
    """

    def test_head_cap_maps_by_backend(self):
        """PG keeps left() (index-matching); everything else uses substr()."""
        assert _head_cap_sql('pg') == 'left(search_text, 10000)'
        assert _head_cap_sql('sqlite') == 'substr(search_text, 1, 10000)'
        # Any non-pg backend string falls to the portable form (fail-safe).
        assert _head_cap_sql('') == 'substr(search_text, 1, 10000)'

    def test_sqlite_left_form_raises_but_substr_form_works(self):
        """Execute both head-cap forms against real in-memory SQLite.

        This is the crux of the bug: the PG form is a hard runtime error on
        SQLite, while the chosen SQLite form runs and correctly bounds the
        substring match. Proves the fix independent of the (PG) test harness.
        """
        import sqlite3

        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE conversations '
                     '(id TEXT, user_id INTEGER, search_text TEXT, updated_at INTEGER)')
        conn.execute("INSERT INTO conversations VALUES "
                     "('c1', 1, 'the superbacktesting framework is great', 1)")
        conn.commit()

        pattern = '%superbacktest%'

        # 1. The OLD unconditional PG form must fail on SQLite (the bug).
        pg_sql = ('SELECT id FROM conversations WHERE user_id=1 '
                  f'AND lower({_head_cap_sql("pg")}) LIKE ?')
        with pytest.raises(sqlite3.OperationalError) as exc:
            conn.execute(pg_sql, (pattern,)).fetchall()
        assert 'left' in str(exc.value).lower()

        # 2. The NEW SQLite form runs and finds the substring match.
        sqlite_sql = ('SELECT id FROM conversations WHERE user_id=1 '
                      f'AND lower({_head_cap_sql("sqlite")}) LIKE ?')
        rows = conn.execute(sqlite_sql, (pattern,)).fetchall()
        assert [r[0] for r in rows] == ['c1'], (
            'substr() head-cap form must find the substring match on SQLite')
        conn.close()

    def test_head_cap_bounds_to_10000_chars_on_sqlite(self):
        """The substr cap actually bounds the scan: a match past char 10000
        (beyond the cap) is NOT found, mirroring the PG left()-cap semantics."""
        import sqlite3

        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE conversations (id TEXT, search_text TEXT)')
        # Marker sits AFTER the 10000-char cap → must be excluded.
        far = 'x' * 10000 + ' needle_far'
        near = 'needle_near ' + 'y' * 20
        conn.execute("INSERT INTO conversations VALUES ('far', ?)", (far,))
        conn.execute("INSERT INTO conversations VALUES ('near', ?)", (near,))
        conn.commit()

        cap = _head_cap_sql('sqlite')
        got_far = conn.execute(
            f'SELECT id FROM conversations WHERE lower({cap}) LIKE ?',
            ('%needle_far%',)).fetchall()
        got_near = conn.execute(
            f'SELECT id FROM conversations WHERE lower({cap}) LIKE ?',
            ('%needle_near%',)).fetchall()
        conn.close()
        assert got_far == [], 'match beyond the 10000-char cap must be excluded'
        assert [r[0] for r in got_near] == ['near']

    def test_snippet_projection_is_bounded_and_sqlite_executable(self):
        """Only the short snippet, never the full search blob, is returned."""
        import sqlite3

        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE conversations '
                     '(id TEXT, user_id INTEGER, search_text TEXT)')
        huge = ('a' * 200_000) + ' needle_here ' + ('z' * 200_000)
        conn.execute('INSERT INTO conversations VALUES (?, ?, ?)',
                     ('huge', 1, huge))
        radius = 40
        width = (2 * radius) + len('needle_here')
        sql = _snippet_projection_sql('sqlite', '?')
        row = conn.execute(
            sql, (radius, width, 'needle_here', 1, 'huge')
        ).fetchone()
        conn.close()

        assert row is not None and 'needle_here' in row['snippet']
        assert len(row['snippet']) <= width
        assert len(row['snippet']) * 1000 < len(huge), (
            'snippet projection accidentally returned a large fraction of '
            'the authoritative search blob')

    def test_pg_snippet_projection_uses_server_side_substring(self):
        sql = _snippet_projection_sql('pg', '?,?')
        assert sql.startswith('SELECT id, CASE WHEN match_pos > 0')
        assert 'substring(search_text FROM GREATEST' in sql
        assert "strpos(lower(search_text), ?)" in sql
        assert 'AS snippet' in sql
