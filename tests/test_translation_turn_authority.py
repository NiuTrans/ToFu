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
