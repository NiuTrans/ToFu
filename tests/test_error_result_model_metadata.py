"""tests/test_error_result_model_metadata.py — dispatch-level failures must
persist metadata.model.

PRODUCTION GAP (epic pt_8f6cbc753855415e): 40 error rows in 14 days carried
``metadata`` WITHOUT a ``model`` key (keys were literally
``['finishReason', 'taskId']``) — the revoked-OAuth 401 cluster and the
endpoint-unreachable exhaustions. Per-model failure-rate stats group on
``metadata->>'model'``, so every dispatch-level failure was invisible.

ROOT CAUSE: ``task['model']`` was stamped only AFTER a successful round
(``orchestrator/_run.py`` loop tail, "Surface the resolved model … AS SOON as
it's known") or at finalization. A first-call dispatch failure (401 revoked
slot / all keys cooling / endpoint unreachable) raises BEFORE any round
succeeds → the error persist saw ``task.get('model')`` unset →
``build_result_meta`` omitted the key.

FIX: seed ``task['model'] = model`` in run_task Section 1, immediately after
``_resolve_model_config`` resolves it — the earliest point the value exists.
The post-round stamp still tracks fallback swaps.

NEUTER: deleting the Section-1 seed is exactly the pre-fix state — the
ground-truth test goes red (proven failing-first before the fix landed).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_error_result_model_metadata.py -p no:cacheprovider
"""

from __future__ import annotations

import json as _json
import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/error_model_meta_unittest.db')


def _seed_conv(conv_id):
    import time as _time

    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [
        {'role': 'user', 'content': 'hi', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(_time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'err-model-meta',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _cleanup(conv_id, task_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))
        db.commit()
    except Exception:
        pass


def _run_failing_task(monkeypatch, conv_id):
    """Drive a REAL run_task whose FIRST LLM call dies at dispatch level
    (the revoked-OAuth 401 shape: a non-retryable exception before any token).
    Returns (task, persisted_metadata_dict)."""
    import lib.tasks_pkg.llm_fallback as llm_fb
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.orchestrator as orch

    class _DispatchDead(Exception):
        """Stand-in for the non-retryable dispatch failure (401/all-slots-dead)."""

    def _stub_raise(task, body, tag='', on_tool_call_ready=None):
        raise _DispatchDead('OAuth access token has been revoked')

    for mod in (mgr, orch, llm_fb):
        if hasattr(mod, 'stream_llm_response'):
            monkeypatch.setattr(mod, 'stream_llm_response', _stub_raise)

    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator import run_task
    task = create_task(
        conv_id,
        [{'role': 'user', 'content': 'hi'}],
        {'model': 'yuju-claude-opus-5-evaDaily', 'projectEnabled': False},
    )
    try:
        run_task(task)
    except Exception:
        pass  # run_task's own terminal handling may re-raise; the row is what matters

    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT status, metadata FROM task_results WHERE task_id=?', (task['id'],)
    ).fetchone()
    assert row, 'no task_results row persisted for the failed task'
    meta = row[1]
    if isinstance(meta, str):
        meta = _json.loads(meta) if meta else {}
    return task, row[0], (meta or {})


@pytest.fixture()
def _db():
    from lib.database import init_db
    try:
        init_db()
    except Exception as e:
        pytest.skip(f'DB bootstrap unavailable in this env ({type(e).__name__}: {e})')
    yield


def test_dispatch_failure_persists_model_in_metadata(monkeypatch, _db):
    """GROUND TRUTH: an error row from a first-call dispatch failure MUST
    carry metadata.model — per-model failure stats depend on it."""
    conv_id = 'cv-errmodel-' + os.urandom(4).hex()
    _seed_conv(conv_id)
    task, status, meta = _run_failing_task(monkeypatch, conv_id)
    try:
        assert status == 'error', f'expected terminal error status, got {status!r}'
        assert meta.get('model') == 'yuju-claude-opus-5-evaDaily', (
            f'metadata.model missing/wrong on a dispatch-level failure '
            f'(got {meta.get("model")!r}, keys={sorted(meta.keys())}) — this is '
            'the 40-null-model-row stats blindness from production')
    finally:
        _cleanup(conv_id, task['id'])


def test_successful_round_still_stamps_model(monkeypatch, _db):
    """Regression: the happy path (round succeeds) keeps recording the model —
    the Section-1 seed must not break the existing post-round stamp."""
    import lib.tasks_pkg.llm_fallback as llm_fb
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.orchestrator as orch

    def _stub_ok(task, body, tag='', on_tool_call_ready=None):
        with task['content_lock']:
            task['content'] += 'hello'
        mgr.append_event(task, mgr.build_event(mgr.EventType.DELTA, content='hello'))
        return ({'role': 'assistant', 'content': 'hello', 'tool_calls': []},
                'stop',
                {'prompt_tokens': 5, 'completion_tokens': 1, 'total_tokens': 6})

    for mod in (mgr, orch, llm_fb):
        if hasattr(mod, 'stream_llm_response'):
            monkeypatch.setattr(mod, 'stream_llm_response', _stub_ok)

    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator import run_task
    conv_id = 'cv-okmodel-' + os.urandom(4).hex()
    _seed_conv(conv_id)
    task = create_task(
        conv_id,
        [{'role': 'user', 'content': 'hi'}],
        {'model': 'kimi-k3', 'projectEnabled': False},
    )
    try:
        run_task(task)
        assert task.get('model') == 'kimi-k3'
        assert task.get('status') == 'done', f'happy path broke: {task.get("status")}'
    finally:
        _cleanup(conv_id, task['id'])


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
