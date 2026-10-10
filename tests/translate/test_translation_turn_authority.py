"""Translation persistence has exactly one owner-scoped turn authority."""

from pathlib import Path

import pytest

from lib.storage import StorageSupervisor


pytestmark = pytest.mark.unit


@pytest.fixture()
def turn_store(tmp_path: Path, monkeypatch):
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend="sqlite", startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(
        "lib.storage.get_storage_client",
        lambda write=False: supervisor.client,
    )
    try:
        yield supervisor
    finally:
        supervisor.stop()


def _settled_turn(turn_store, conversation_id="translation-conv"):
    from lib.turn_lifecycle import create_turn_pair, record_task_event

    created = create_turn_pair(
        conversation_id,
        command_id=f"{conversation_id}-command",
        input_projection={"content": "question"},
        config={},
        user_id=1,
        conversation_defaults={
            "allowCreate": True,
            "title": "Translation",
            "settings": {},
        },
    )
    task = {
        "id": "translation-executor",
        "_attemptId": created["attempt"]["attemptId"],
        "_turnId": created["turn"]["turnId"],
        "_userId": 1,
        "status": "running",
        "content": "Final answer",
        "thinking": "",
        "toolRounds": [],
        "segments": [
            {
                "type": "text",
                "text": "I will inspect it",
                "deliverable": False,
                "llmRound": 1,
            },
            {
                "type": "text",
                "text": "Final answer",
                "deliverable": True,
                "terminal": True,
                "llmRound": 2,
            },
            {
                "type": "thinking",
                "blockId": "thinking:llm-1",
                "text": "reason one",
                "deliverable": False,
                "llmRound": 1,
            },
        ],
        "config": {},
    }
    assert record_task_event(task, {"type": "done", "finishReason": "stop"})
    return created


def test_translation_merges_into_turn_projection(turn_store):
    from lib.translate.commit import commit_translation_to_turn
    from lib.turn_lifecycle import get_turn

    created = _settled_turn(turn_store)
    turn_id = created["turn"]["turnId"]
    commit_translation_to_turn(
        "translation-conv",
        turn_id,
        "translatedContent",
        "最终答案",
        user_id=1,
        model="translator-model",
        segment_translations={1: "我会检查它", "thinking:llm-1": "推理一"},
    )

    projection = get_turn(
        "translation-conv", turn_id, user_id=1)["projection"]
    assert projection["content"] == "Final answer"
    assert projection["translatedContent"] == "最终答案"
    assert projection["translation"]["status"] == "completed"
    assert projection["translation"]["model"] == "translator-model"
    assert projection["segments"][0]["translatedText"] == "我会检查它"
    assert "translatedText" not in projection["segments"][1]
    # Reasoning resolves by its collision-free blockId, not the round number
    # it shares with the narration prose.
    assert projection["segments"][2]["translatedText"] == "推理一"
    archive = turn_store.client.query("conversation.get", {
        "conv_id": "translation-conv", "user_id": 1,
    })
    assistant = next(
        message for message in archive["messages"]
        if message.get("role") == "assistant"
    )
    assert assistant["translatedContent"] == "最终答案"
    assert assistant["translation"]["status"] == "completed"


def test_segment_translation_block_id_is_attempt_scoped():
    from lib.translate.commit import _stamp_segment_translations

    projection = {
        'segments': [
            {
                'type': 'text', 'deliverable': False,
                'blockId': 'text:attempt-old:llm-0',
                'attemptId': 'old', 'llmRound': 0,
                'text': 'old narration',
            },
            {
                'type': 'text', 'deliverable': False,
                'blockId': 'text:attempt-new:llm-0',
                'attemptId': 'new', 'llmRound': 0,
                'text': 'new narration',
            },
            {
                'type': 'text', 'deliverable': True, 'terminal': True,
                'blockId': 'text:terminal', 'text': 'final answer',
            },
        ],
    }

    stamped = _stamp_segment_translations(
        projection,
        {'text:attempt-new:llm-0': '新旁白'},
    )

    assert stamped == 1
    assert 'translatedText' not in projection['segments'][0]
    assert projection['segments'][1]['translatedText'] == '新旁白'
    assert 'translatedText' not in projection['segments'][2]


def test_stale_translation_retry_preserves_sibling_projection_change(
        turn_store, monkeypatch):
    import lib.translate.commit as commit
    from lib.turn_lifecycle import get_turn, update_turn_projection

    created = _settled_turn(turn_store, "translation-race")
    turn_id = created["turn"]["turnId"]
    real_update = commit.update_turn_projection
    first = True

    def collide(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            latest = get_turn("translation-race", turn_id, user_id=1)
            sibling = dict(latest["projection"])
            sibling["preset"] = "review-approved"
            update_turn_projection(
                "translation-race",
                turn_id,
                projection=sibling,
                expected_projection_revision=latest["projectionRevision"],
                user_id=1,
            )
        return real_update(*args, **kwargs)

    monkeypatch.setattr(commit, "update_turn_projection", collide)
    commit.commit_translation_to_turn(
        "translation-race", turn_id, "translatedContent", "译文", user_id=1)

    projection = get_turn("translation-race", turn_id, user_id=1)["projection"]
    assert projection["preset"] == "review-approved"
    assert projection["translatedContent"] == "译文"


def test_owner_scope_is_enforced(turn_store):
    from lib.translate.commit import commit_translation_to_turn
    from lib.turn_lifecycle import LifecycleNotFound

    created = _settled_turn(turn_store, "translation-owner")
    with pytest.raises(LifecycleNotFound):
        commit_translation_to_turn(
            "translation-owner",
            created["turn"]["turnId"],
            "translatedContent",
            "must not land",
            user_id=2,
        )


def test_retryable_storage_timeout_is_retried(turn_store, monkeypatch):
    import lib.translate.commit as commit
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import get_turn

    created = _settled_turn(turn_store, "translation-timeout")
    turn_id = created["turn"]["turnId"]
    real_update = commit.update_turn_projection
    calls = {"n": 0}

    def flaky_update(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise StorageError(
                "database_timeout",
                "Storage writer acquisition timed out",
                True,
                25,
            )
        return real_update(*args, **kwargs)

    monkeypatch.setattr(commit, "update_turn_projection", flaky_update)
    monkeypatch.setattr(commit.time, "sleep", lambda _seconds: None)

    commit.commit_translation_to_turn(
        "translation-timeout", turn_id, "translatedContent", "译文",
        user_id=1, model="translator-model",
    )

    assert calls["n"] == 3
    projection = get_turn(
        "translation-timeout", turn_id, user_id=1)["projection"]
    assert projection["translatedContent"] == "译文"


def test_retryable_storage_timeout_surfaces_after_exhaustion(
        turn_store, monkeypatch):
    import lib.translate.commit as commit
    from lib.storage.errors import StorageError

    created = _settled_turn(turn_store, "translation-exhaust")
    turn_id = created["turn"]["turnId"]
    calls = {"n": 0}

    def always_timeout(*args, **kwargs):
        calls["n"] += 1
        raise StorageError(
            "database_timeout", "Storage writer acquisition timed out",
            True, 25,
        )

    monkeypatch.setattr(commit, "update_turn_projection", always_timeout)
    monkeypatch.setattr(commit.time, "sleep", lambda _seconds: None)

    with pytest.raises(StorageError, match="writer acquisition"):
        commit.commit_translation_to_turn(
            "translation-exhaust", turn_id, "translatedContent", "译文",
            user_id=1,
        )
    assert calls["n"] == commit._MAX_STORAGE_ATTEMPTS


def test_non_retryable_storage_error_is_not_retried(turn_store, monkeypatch):
    import lib.translate.commit as commit
    from lib.storage.errors import StorageError

    created = _settled_turn(turn_store, "translation-fatal")
    turn_id = created["turn"]["turnId"]
    calls = {"n": 0}

    def fatal(*args, **kwargs):
        calls["n"] += 1
        raise StorageError("database_internal", "corrupt", False, None)

    monkeypatch.setattr(commit, "update_turn_projection", fatal)
    monkeypatch.setattr(commit.time, "sleep", lambda _seconds: None)

    with pytest.raises(StorageError, match="corrupt"):
        commit.commit_translation_to_turn(
            "translation-fatal", turn_id, "translatedContent", "译文",
            user_id=1,
        )
    assert calls["n"] == 1


def test_cached_segment_sources_fence_replaced_terminal_reasoning():
    from lib.translate.commit import _stamp_segment_translations

    projection = {'segments': [
        {'type': 'thinking', 'blockId': 'thinking:terminal',
         'text': 'New attempt reasoning'},
        {'type': 'thinking', 'blockId': 'thinking:attempt-old:llm-0',
         'text': 'Kept reasoning'},
    ]}
    assert _stamp_segment_translations(
        projection,
        {'thinking:terminal': '旧终态推理', 'thinking:attempt-old:llm-0': '保留推理'},
        segment_sources={
            'thinking:terminal': 'Old attempt reasoning',
            'thinking:attempt-old:llm-0': 'Kept reasoning',
        },
    ) == 1
    assert 'translatedText' not in projection['segments'][0]
    assert projection['segments'][1]['translatedText'] == '保留推理'


@pytest.mark.parametrize('source, translated', [
    ('Completed reasoning', '已经完成的推理'),
    ('Complete reasoning. ' * 10_000, '完整推理译文。' * 20_000),
], ids=['ordinary', 'long-file-backed'])
def test_idle_cache_commits_after_real_turn_settlement_and_survives_cold_read(
        turn_store, monkeypatch, source, translated):
    import lib.translate.incremental as incremental
    from lib.tool_round_identity import model_batch_segment_block_id
    from lib.turn_lifecycle import create_turn_pair, get_turn, record_task_event

    conversation_id = 'translation-retirement-authority'
    created = create_turn_pair(
        conversation_id, command_id='retirement-command',
        input_projection={'content': 'question'}, config={}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Translation',
                               'settings': {}},
    )
    attempt_id = created['attempt']['attemptId']
    turn_id = created['turn']['turnId']
    key = model_batch_segment_block_id('thinking', 0, attempt_id=attempt_id)
    task = {
        'id': 'retirement-executor', 'convId': conversation_id,
        '_attemptId': attempt_id, '_turnId': turn_id, '_userId': 1,
        'status': 'running', 'content': '', 'thinking': '', 'toolRounds': [],
        'segments': [{'type': 'thinking', 'blockId': key, 'llmRound': 0,
                      'text': source}],
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    accumulator = incremental._Accumulator(task)
    monkeypatch.setattr(
        accumulator, '_translate', lambda *a, **kw: (translated, 'translator'),
    )
    monkeypatch.setattr(accumulator, '_push', lambda _payload: None)
    accumulator._process_preview_segment(key, source)
    monkeypatch.setattr(incremental, '_accumulators', {task['id']: accumulator})

    accumulator._run()
    # Real storage still fences live writes; retiring the worker retains cache.
    live_segments = get_turn(
        conversation_id, turn_id, user_id=1)['projection'].get('segments') or []
    assert all('translatedText' not in s for s in live_segments)
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    assert incremental.finalize_incremental_stamp_only(task) is True
    assert incremental._TASK_PREVIEW_STATE not in task

    cold = turn_store.client.query('conversation.get', {
        'conv_id': conversation_id, 'user_id': 1,
    })
    assistant = next(m for m in cold['messages'] if m.get('role') == 'assistant')
    segment = next(s for s in assistant['segments'] if s.get('blockId') == key)
    assert segment['translatedText'] == translated
    assert 'translatedContent' not in assistant


def test_segment_source_guard_rechecks_latest_projection_on_cas_conflict(
        turn_store, monkeypatch):
    import lib.translate.commit as commit
    from lib.turn_lifecycle import get_turn, update_turn_projection

    created = _settled_turn(turn_store, 'translation-source-race')
    turn_id = created['turn']['turnId']
    real_update = commit.update_turn_projection
    first = True

    def collide(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            latest = get_turn('translation-source-race', turn_id, user_id=1)
            sibling = latest['projection']
            thinking = next(s for s in sibling['segments'] if s.get('type') == 'thinking')
            thinking['text'] = 'Edited reasoning'
            sibling['preset'] = 'concurrent-user-edit'
            update_turn_projection(
                'translation-source-race', turn_id, projection=sibling,
                expected_projection_revision=latest['projectionRevision'], user_id=1,
            )
        return real_update(*args, **kwargs)

    monkeypatch.setattr(commit, 'update_turn_projection', collide)
    commit.commit_translation_to_turn(
        'translation-source-race', turn_id, None, '', user_id=1,
        segment_translations={'thinking:llm-1': '旧推理译文'},
        segment_sources={'thinking:llm-1': 'reason one'},
    )
    projection = get_turn('translation-source-race', turn_id, user_id=1)['projection']
    thinking = next(s for s in projection['segments'] if s.get('type') == 'thinking')
    assert thinking['text'] == 'Edited reasoning'
    assert 'translatedText' not in thinking
    assert projection['preset'] == 'concurrent-user-edit'


@pytest.mark.parametrize("kind", ["success", "failure", "noop", "segments"])
def test_translation_overlay_preserves_activity_and_advances_revision(turn_store, kind):
    from lib.translate.commit import (commit_translation_to_turn, mark_turn_translation_failed,
                                      mark_turn_translation_complete)
    from lib.turn_lifecycle import get_turn, update_turn_projection
    created = _settled_turn(turn_store)
    turn_id = created["turn"]["turnId"]
    query = {"conv_id": "translation-conv", "user_id": 1, "derive_messages": False}
    # Pin an unmistakably old activity time through the owner-scoped authority.
    turn_store.client.command("conversation.metadata.update", {
        "conv_id": "translation-conv", "user_id": 1,
        "updates": {"updated_at": 1700000000000},
    }, f"pin-activity-{kind}")
    before = turn_store.client.query("conversation.get", query)["metadata"]
    if kind == "failure":
        mark_turn_translation_failed("translation-conv", turn_id, user_id=1,
                                     reason="timeout", error="deadline exhausted")
    elif kind == "noop":
        mark_turn_translation_complete("translation-conv", turn_id, user_id=1)
    else:
        commit_translation_to_turn("translation-conv", turn_id,
            "translatedContent" if kind == "success" else None,
            "最终答案" if kind == "success" else "", user_id=1,
            segment_translations={"thinking:llm-1": "推理一"})
    after = turn_store.client.query("conversation.get", query)["metadata"]
    assert after["updated_at"] == before["updated_at"]
    assert after["rev"] > before["rev"]
    turn = get_turn("translation-conv", turn_id, user_id=1)
    update_turn_projection("translation-conv", turn_id, user_id=1,
        projection={**turn["projection"], "content": "User edited answer"},
        expected_projection_revision=turn["projectionRevision"])
    edited = turn_store.client.query("conversation.get", query)["metadata"]
    assert edited["updated_at"] > after["updated_at"]
