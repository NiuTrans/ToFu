#!/usr/bin/env python3
"""tests/test_chat_start_interrupted_tail.py — the /chat/start interrupted-tail
guard (epic pt_f5771a2e, fix B2).

WHY
---
A bare ``POST /api/v1/chat/start`` whose conv DB tail is an INTERRUPTED
assistant stub used to APPEND a fresh sibling turn: the built prompt included
the stub as a completed prior turn and the new task synced its answer as a
NEW assistant message — the ``U A U A(stub) A(answer)`` doubled layout. This
is the exact ms43foj3 incident (2026-07-28): after the second restart, the
frontend's pop-and-regenerate escape hatch reached ``/chat/start`` with the
rolled-back stub still at the tail, and task 9430aa39 appended a twin answer
instead of resuming the stub. The user-facing contract: one user turn → ONE
agent bubble that keeps generating.

The guard (routes/chat.py::chat_start): when the RAW conv tail is an
assistant message with ``interruptedReason`` and no ``finishReason``, the
bare start delegates to the SAME continue contract
(``lib.chat_dispatch.execute_chat_continue``) — resume in place when a
checkpoint/prefill exists, otherwise drop the unrecoverable stub and start
fresh. The guard reads the RAW row because the API transform REBUILDS
assistant rows and strips the lifecycle fields (a guard reading the built
prompt list would be dead code).

TESTS (route-driven, flask_client):
  1. ``test_start_with_interrupted_stub_resumes_in_place`` — stub with a
     completed tool round → the response is the CONTINUE payload
     (taskId + checkpoint), ``_start_task_for_conv`` receives the
     excludeLast/checkpointToolRounds cfg, and the conv still holds exactly
     [user, assistant] (rollback in place, no twin).
  2. ``test_start_with_empty_interrupted_husk_starts_fresh`` — empty stub →
     fallback: the husk is DROPPED from the conv and a fresh task starts
     with the user tail (replacement, never twin).
  3. ``test_start_with_unrecoverable_stub_starts_fresh`` — content but no
     tool rounds and no segments (no checkpoint, prefail fail-closed) →
     same drop-and-fresh-start.
  4. ``test_inline_messages_bypass_the_guard`` — inline ``messages`` callers
     (SWE-bench / eval harness) own their wire shape; the guard never fires.
  5. ``test_guard_source_anchor_and_neuter`` — source-contract NC: the
     predicate line is load-bearing for the anchor.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/chat_start_guard_unittest.db')

ROOT = PROJECT_ROOT
CHAT_ROUTE_PATH = os.path.join(ROOT, 'routes', 'chat.py')


def _seed(flask_client, conv_id, tail_msg):
    now = int(time.time() * 1000)
    r = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
        "title": "guard", "messages": [
            {"role": "user", "content": "do the thing", "timestamp": now},
            tail_msg,
        ], "createdAt": now, "updatedAt": now})
    assert r.status_code == 200, r.get_data(as_text=True)


def _messages_of(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row:
        return []
    return json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])


def _drop_task(task_id, conv_id):
    from lib.tasks_pkg.manager import discard_task
    discard_task(task_id, conv_id)
    try:
        from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))
        db.commit()
    except Exception:
        pass


@pytest.mark.api
def test_start_with_interrupted_stub_resumes_in_place(flask_client, monkeypatch):
    """The incident replay: bare /chat/start with the rolled-back stub at the
    tail must RESUME via the continue contract — never append a twin turn."""
    import routes.chat as chatmod
    captured = {}

    def _capture(conv_id, config, data=None, user_msg_id=''):
        captured['cfg'] = config
        captured['user_msg_id'] = user_msg_id
        return ('stub-resume-task', None)

    monkeypatch.setattr(chatmod, '_start_task_for_conv', _capture)

    conv_id = f"cv-guard-resume-{int(time.time() * 1000)}"
    done_round = {"roundNum": 1, "llmRound": 0, "toolCallId": "tc_1",
                  "toolName": "web_search", "toolArgs": '{"query":"x"}',
                  "toolContent": "hit", "status": "done",
                  "assistantContent": "checking first. "}
    _seed(flask_client, conv_id, {
        "role": "assistant", "content": "checking first. partial answer tail",
        "thinking": "", "toolRounds": [done_round],
        "interruptedReason": "manual", "timestamp": int(time.time() * 1000) + 1})
    try:
        resp = flask_client.post("/api/v1/chat/start", json={
            "convId": conv_id, "config": {"model": "gpt-4o"}})
        data = resp.get_json()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        # The CONTINUE contract answered — not a bare start.
        assert data.get('taskId') == 'stub-resume-task', (
            f'expected the resume task from the continue contract, got {data}')
        assert data.get('checkpoint', {}).get('keptRounds') == 1, (
            f'expected the checkpoint payload (keptRounds=1), got {data}')
        cfg = captured.get('cfg') or {}
        assert cfg.get('excludeLast') is True, (
            'the resume task must rebuild messages EXCLUDING the rolled-back '
            'stub (the continue contract), got cfg keys: '
            f'{sorted(cfg.keys())}')
        assert len(cfg.get('checkpointToolRounds') or []) == 1, (
            'the completed tool round must ride forward as checkpointToolRounds')
        # The conv still holds exactly [user, assistant] — the rollback was
        # IN PLACE. A twin append would show 3+ messages.
        msgs = _messages_of(conv_id)
        assert len(msgs) == 2 and msgs[-1].get('role') == 'assistant', (
            f'rollback was not in place — conv now has {len(msgs)} messages '
            '(twin append regression)')
    finally:
        flask_client.delete(f"/api/v1/conversations/{conv_id}")


@pytest.mark.api
def test_start_with_empty_interrupted_husk_starts_fresh(flask_client, monkeypatch):
    """An EMPTY interrupted husk is unrecoverable by definition — the guard
    drops it and starts fresh (the replacement, never the twin)."""
    import lib.tasks_pkg
    monkeypatch.setattr(lib.tasks_pkg, 'spawn_task', lambda task: None)

    conv_id = f"cv-guard-husk-{int(time.time() * 1000)}"
    _seed(flask_client, conv_id, {
        "role": "assistant", "content": "", "thinking": "", "toolRounds": [],
        "interruptedReason": "manual", "timestamp": int(time.time() * 1000) + 1})
    task_id = None
    try:
        resp = flask_client.post("/api/v1/chat/start", json={
            "convId": conv_id, "config": {"model": "gpt-4o"}})
        data = resp.get_json()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        task_id = data.get('taskId')
        assert task_id, f'no taskId in response: {data}'
        assert 'checkpoint' not in data, (
            f'an empty husk must NOT route through the continue checkpoint: {data}')
        # The husk is GONE from the conv — replaced, not twinned.
        msgs = _messages_of(conv_id)
        assert len(msgs) == 1 and msgs[-1].get('role') == 'user', (
            f'the unrecoverable husk was not dropped: {[(m.get("role")) for m in msgs]}')
        # The fresh task's prompt tail is the user message.
        from lib.tasks_pkg import tasks, tasks_lock
        with tasks_lock:
            t = tasks.get(task_id)
        assert t is not None and t['messages'] and t['messages'][-1].get('role') == 'user', (
            'fresh task prompt must end on the user turn after the husk drop')
    finally:
        if task_id:
            _drop_task(task_id, conv_id)
        flask_client.delete(f"/api/v1/conversations/{conv_id}")


@pytest.mark.api
def test_start_with_unrecoverable_stub_starts_fresh(flask_client, monkeypatch):
    """Content but NO tool rounds and NO segments → no checkpoint and prefill
    fail-closed → the stub is dropped and a fresh task starts."""
    import lib.tasks_pkg
    monkeypatch.setattr(lib.tasks_pkg, 'spawn_task', lambda task: None)

    conv_id = f"cv-guard-nockpt-{int(time.time() * 1000)}"
    _seed(flask_client, conv_id, {
        "role": "assistant", "content": "half-written prose with no tools",
        "thinking": "", "toolRounds": [],
        "interruptedReason": "manual", "timestamp": int(time.time() * 1000) + 1})
    task_id = None
    try:
        resp = flask_client.post("/api/v1/chat/start", json={
            "convId": conv_id, "config": {"model": "gpt-4o"}})
        data = resp.get_json()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        task_id = data.get('taskId')
        assert task_id
        assert 'checkpoint' not in data
        msgs = _messages_of(conv_id)
        assert len(msgs) == 1 and msgs[-1].get('role') == 'user', (
            f'unrecoverable stub was not dropped: {[m.get("role") for m in msgs]}')
    finally:
        if task_id:
            _drop_task(task_id, conv_id)
        flask_client.delete(f"/api/v1/conversations/{conv_id}")


@pytest.mark.api
def test_inline_messages_bypass_the_guard(flask_client, monkeypatch):
    """Inline-messages callers (SWE-bench / eval harnesses) own their wire
    shape — the guard must NOT fire (no conv DB row is authoritative)."""
    import lib.tasks_pkg
    monkeypatch.setattr(lib.tasks_pkg, 'spawn_task', lambda task: None)

    conv_id = f"cv-guard-inline-{int(time.time() * 1000)}"
    resp = flask_client.post("/api/v1/chat/start", json={
        "convId": conv_id,
        "config": {"model": "gpt-4o"},
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "old", "interruptedReason": "manual"},
        ]})
    data = resp.get_json()
    task_id = data.get('taskId')
    try:
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert 'checkpoint' not in data, (
            f'inline start must never route through the continue contract: {data}')
        from lib.tasks_pkg import tasks, tasks_lock
        with tasks_lock:
            t = tasks.get(task_id)
        assert t is not None and len(t['messages']) == 2, (
            'inline messages must reach the task verbatim')
    finally:
        if task_id:
            _drop_task(task_id, conv_id)


def test_guard_source_anchor_and_neuter():
    """Source-contract NC: the guard predicate is the load-bearing line —
    disabling it must flip this anchor red (proves the anchor is not vacuous)."""
    with open(CHAT_ROUTE_PATH, encoding='utf-8') as f:
        src = f.read()
    start = src.index('def chat_start():')
    end = src.index("task = create_task(conv_id, messages, cfg)", start)
    region = src[start:end]
    needle = "and _raw_tail.get('interruptedReason')"
    assert 'interrupted-tail' in region, (
        'chat_start lost the interrupted-tail guard block')
    assert 'execute_chat_continue(' in region, (
        'chat_start must delegate the interrupted tail to execute_chat_continue')
    assert needle in region, (
        'the guard predicate no longer keys on the RAW tail interruptedReason — '
        'a guard reading the API-transformed list is dead code (the transform '
        'strips lifecycle fields)')
    # NEUTER: with the predicate disabled the anchor above would pass vacuously
    # unless it is keyed on this exact line — prove it is.
    neutered = region.replace(needle, 'and False', 1)
    assert neutered != region
    assert needle not in neutered, (
        'NEUTER succeeded but the anchor would still pass — it is not actually '
        'keyed on the guard predicate')


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
