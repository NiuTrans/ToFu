"""Whole-paper translation persists via its named storage.v1 operation."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_translate_worker_uses_semantic_translation_upsert(monkeypatch):
    import lib.paper.translate_engine as engine
    from lib.paper import translate_runtime as runtime

    calls = []

    class _Client:
        def command(self, operation, payload, command_id):
            calls.append((operation, payload, command_id))
            return {'saved': True}

    def dispatch(_messages, *, on_content=None, **_kwargs):
        on_content('translated paragraph')
        return ({'role': 'assistant'}, 'stop', {})

    monkeypatch.setattr(engine, 'dispatch_stream', dispatch)
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: _Client())
    task_id = 'translate-sidecar-contract'
    task = runtime._new_translate_task(
        task_id, 'paper-translation', 'review:neurips:zh', 'model-a')
    try:
        engine._run_translate_task(task, 'source paragraph')
    finally:
        with runtime._translate_runtime._lock:
            runtime._translate_runtime._tasks.pop(task_id, None)
        with runtime._translate_dedup_lock:
            runtime._translate_dedup_index.pop(
                ('paper-translation', 'review:neurips:zh'), None)

    assert task['status'] == 'done'
    operation, payload, command_id = calls[0]
    assert operation == 'paper.translation.upsert'
    assert payload['paper_hash'] == 'paper-translation'
    assert payload['lang'] == 'review:neurips:zh'
    assert payload['text'] == 'translated paragraph'
    assert payload['model'] == 'model-a'
    assert command_id.startswith('paper.translation.upsert:')
