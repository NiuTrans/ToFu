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

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

def _seed_conv(conv_id):
    from tests._seed import seed_conversation
    messages = [
        {'role': 'user', 'content': 'hi', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    seed_conversation(conv_id, messages=messages, title='err-model-meta')


def _cleanup(conv_id, task_id):
    from lib.storage import get_storage_client
    from tests._seed import delete_conversation
    try:
        delete_conversation(conv_id)
        get_storage_client(write=True).command(
            'record.delete', {'namespace': 'task_results', 'key': task_id},
            f'test-delete-task-result:{task_id}')
    except Exception:
        pass


def _run_failing_task(monkeypatch, conv_id):
    """Drive a REAL run_task whose FIRST LLM call dies at dispatch level
    (the revoked-OAuth 401 shape: a non-retryable exception before any token).
    Returns (task, persisted_metadata_dict)."""
    import lib.tasks_pkg.llm_fallback._call as llm_fb

    class _DispatchDead(Exception):
        """Stand-in for the non-retryable dispatch failure (401/all-slots-dead)."""

    def _stub_raise(task, body, tag='', on_tool_call_ready=None):
        raise _DispatchDead('OAuth access token has been revoked')

    monkeypatch.setattr(llm_fb, 'stream_llm_response', _stub_raise)

    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator.api import run_task
    task = create_task(
        conv_id,
        [{'role': 'user', 'content': 'hi'}],
        {'model': 'yuju-claude-opus-5-evaDaily', 'projectEnabled': False},
    user_id=1,
    )
    try:
        run_task(task)
    except Exception:
        pass  # run_task's own terminal handling may re-raise; the row is what matters

    from lib.storage import get_storage_client
    row = get_storage_client().query(
        'record.get', {'namespace': 'task_results', 'key': task['id']})
    assert row, 'no task_results row persisted for the failed task'
    value = row['value']
    meta = value.get('metadata')
    if isinstance(meta, str):
        meta = _json.loads(meta) if meta else {}
    return task, value.get('status'), (meta or {})


def test_dispatch_failure_persists_model_in_metadata(monkeypatch):
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


def test_successful_round_still_stamps_model(monkeypatch):
    """Regression: the happy path (round succeeds) keeps recording the model —
    the Section-1 seed must not break the existing post-round stamp."""
    from lib.agent_core.events import EventType, build_event
    import lib.tasks_pkg.llm_fallback._call as llm_fb
    from lib.tasks_pkg.manager._events import append_event

    def _stub_ok(task, body, tag='', on_tool_call_ready=None):
        with task['content_lock']:
            task['content'] += 'hello'
        append_event(task, build_event(EventType.DELTA, content='hello'))
        return ({'role': 'assistant', 'content': 'hello', 'tool_calls': []},
                'stop',
                {'prompt_tokens': 5, 'completion_tokens': 1, 'total_tokens': 6})

    monkeypatch.setattr(llm_fb, 'stream_llm_response', _stub_ok)

    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator.api import run_task
    conv_id = 'cv-okmodel-' + os.urandom(4).hex()
    _seed_conv(conv_id)
    task = create_task(
        conv_id,
        [{'role': 'user', 'content': 'hi'}],
        {'model': 'kimi-k3', 'projectEnabled': False},
    user_id=1,
    )
    try:
        run_task(task)
        assert task.get('model') == 'kimi-k3'
        assert task.get('status') == 'done', f'happy path broke: {task.get("status")}'
    finally:
        _cleanup(conv_id, task['id'])


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
