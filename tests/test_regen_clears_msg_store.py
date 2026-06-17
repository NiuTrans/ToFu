"""Regression test for the regen context-accumulation bug.

Bug: ``/api/v1/chat/regenerate`` truncated the conversation's messages in the
DB but left the in-memory ``server_message_store`` (the full-fidelity
tool_use/tool_result history keyed by conv_id, driven by ``keepToolHistory``)
untouched. On the next task the orchestrator's
``rebuild_messages_with_history`` REPLACES the truncated DB-built messages with
that stale stored copy — which still contains the rounds just truncated away —
so every regen replayed an ever-growing context instead of the truncated one.

Fix: ``chat_regenerate`` now calls ``server_message_store.clear(conv_id)`` after
truncating (routes/chat.py, step "5b"). This file pins that behaviour at the
route level.

Run:  pytest tests/test_regen_clears_msg_store.py -m api
"""
from __future__ import annotations

import time

import pytest

from lib.tasks_pkg import server_message_store


def _seed_store(conv_id, n_rounds=3):
    """Populate the store with a stale full-fidelity history for ``conv_id``."""
    messages = [{'role': 'system', 'content': 'sys'}]
    for i in range(n_rounds):
        messages.append({'role': 'user', 'content': f'q{i}'})
        messages.append({
            'role': 'assistant',
            'tool_calls': [{
                'id': f'tc_{i}',
                'type': 'function',
                'function': {'name': 'web_search', 'arguments': '{"query": "x"}'},
            }],
        })
        messages.append({'role': 'tool', 'tool_call_id': f'tc_{i}', 'content': f'result {i}'})
        messages.append({'role': 'assistant', 'content': f'answer {i}'})
    server_message_store.save_messages(conv_id, messages)


@pytest.mark.api
class TestRegenClearsMsgStore:

    def test_regenerate_clears_server_message_store(self, flask_client, monkeypatch):
        conv_id = f"test-regen-store-{int(time.time()*1000)}"
        now = int(time.time() * 1000)

        # 1. Persist a conversation with a couple of turns to the DB.
        save_resp = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Regen store test",
            "messages": [
                {"role": "user", "content": "first question", "timestamp": now},
                {"role": "assistant", "content": "first answer", "timestamp": now + 1},
                {"role": "user", "content": "second question", "timestamp": now + 2},
                {"role": "assistant", "content": "second answer", "timestamp": now + 3},
            ],
            "createdAt": now,
            "updatedAt": now,
        })
        assert save_resp.status_code == 200

        # 2. Seed the in-memory store with stale full history (simulates a
        #    prior turn having saved tool rounds we're about to truncate away).
        _seed_store(conv_id, n_rounds=3)
        assert server_message_store.get_messages(conv_id) is not None

        # 3. Stub task startup so no background thread re-touches the store
        #    after the route clears it — we only assert on the clear itself.
        monkeypatch.setattr('routes.chat._start_task_for_conv',
                            lambda *a, **k: ('stub-task-id', None))

        # 4. Regenerate: truncate back to the first user message (idx 0).
        resp = flask_client.post("/api/v1/chat/regenerate", json={
            "convId": conv_id,
            "truncateToIndex": 0,
            "config": {"model": "mock-model"},
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)

        # 5. The store entry for this conv MUST be gone — otherwise the next
        #    task would replay the stale (longer) history.
        assert server_message_store.get_messages(conv_id) is None, (
            "regenerate did not clear server_message_store — context would "
            "accumulate across regens"
        )

        # Cleanup
        flask_client.delete(f"/api/v1/conversations/{conv_id}")
        server_message_store.clear(conv_id)
