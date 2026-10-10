"""Whole-paper translation persists via its named storage.v1 operation."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1


def test_translate_worker_uses_semantic_translation_upsert(monkeypatch):
    import lib.paper.translate_engine as engine
    import lib.paper.translate_runtime as runtime

    calls = []
    translation_calls = []

    class _Client:
        def command(self, operation, payload, command_id):
            calls.append((operation, payload, command_id))
            return {'saved': True}

    def translate(chunk, system_prompt, **kwargs):
        translation_calls.append((chunk, system_prompt, kwargs))
        return (
            'translated paragraph',
            {'_translate_trace': {'model': 'model-a'}},
        )

    monkeypatch.setattr(engine, '_translate_one_chunk', translate)
    monkeypatch.setattr(
        'lib.storage.get_storage_client', lambda *, write=False: _Client())
    task_id = 'translate-sidecar-contract'
    task = runtime._new_translate_task(
        task_id, 'paper-translation', 'review:neurips:zh', 'model-a', user_id=TEST_OWNER_USER_ID)
    try:
        engine._run_translate_task(task, 'source paragraph')
    finally:
        original_ttl = runtime._translate_runtime.ttl
        runtime._translate_runtime.ttl = -1
        try:
            runtime._cleanup_stale_translate_tasks()
        finally:
            runtime._translate_runtime.ttl = original_ttl

    assert task['status'] == 'done'
    assert len(translation_calls) == 1
    chunk, system_prompt, translate_kwargs = translation_calls[0]
    assert chunk == 'source paragraph'
    assert 'Chinese' in system_prompt
    assert translate_kwargs['prefer_model'] == 'model-a'
    assert translate_kwargs['strict_model'] is True
    assert translate_kwargs['allow_mt'] is False
    assert translate_kwargs['use_cache'] is False
    assert translate_kwargs['stream'] is True
    assert translate_kwargs['capability'] == 'text'
    assert translate_kwargs['temperature'] == 0
    assert translate_kwargs['accept_truncated'] is False
    assert callable(translate_kwargs['abort_check'])
    operation, payload, command_id = calls[0]
    assert operation == 'paper.translation.upsert'
    assert payload['user_id'] == TEST_OWNER_USER_ID
    assert payload['paper_hash'] == 'paper-translation'
    assert payload['lang'] == 'review:neurips:zh'
    assert payload['text'] == 'translated paragraph'
    assert payload['model'] == 'model-a'
    assert command_id.startswith('paper.translation.upsert:')


def test_translate_worker_abort_interrupts_active_dispatch(monkeypatch):
    from lib.llm import AbortedError
    import lib.paper.translate_engine as engine
    import lib.paper.translate_runtime as runtime

    task_id = 'paper-translate-active-abort'
    task = runtime._new_translate_task(
        task_id,
        'paper-abort',
        'zh',
        'model-a',
        user_id=TEST_OWNER_USER_ID,
    )

    def aborting_translate(_chunk, _system_prompt, **kwargs):
        assert callable(kwargs['abort_check'])
        task['abort_event'].set()
        raise AbortedError('owner stopped paper translation')

    monkeypatch.setattr(engine, '_translate_one_chunk', aborting_translate)
    engine._run_translate_task(task, 'source paragraph')

    assert task['status'] == 'aborted'
    assert task['error'] is None
