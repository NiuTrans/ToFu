"""The async worker persists before exposing a terminal result."""

import pytest


pytestmark = pytest.mark.unit


def _register_task(task_id, **fields):
    from lib.translate.runtime import _translate_runtime

    owner_user_id = int(fields.pop("userId", 1))
    task = _translate_runtime.create(
        user_id=owner_user_id,
        task_id=task_id,
    )
    _translate_runtime.mark_running(
        task_id,
        fields={"model": None, **fields},
    )
    return task


def test_bound_worker_commits_before_done_push(monkeypatch):
    import lib.translate.runtime._worker as worker

    _do_translate = worker._do_translate

    order = []
    task = _register_task("worker-bound", userId=7)
    monkeypatch.setattr(
        worker,
        "_translate_freetext",
        lambda *args, **kwargs: (
            "译文", {"_dispatch": {"model": "translator"}},
        ),
    )
    monkeypatch.setattr(
        worker,
        "_build_segment_translation_map",
        lambda *args, **kwargs: {1: "旁白"},
    )
    monkeypatch.setattr(
        worker,
        "commit_translation_to_turn",
        lambda *args, **kwargs: order.append(("commit", args, kwargs)),
    )
    monkeypatch.setattr(
        "lib.agent_core.push.push_event",
        lambda channel, task_id, frame, *, user_id: order.append(("push", frame)),
    )

    _do_translate(
        "worker-bound",
        "answer",
        "Chinese",
        "English",
        "conversation-1",
        "turn-1",
        "translatedContent",
        user_id=7,
        message_id="render-only",
    )

    terminal_push = next(
        index for index, item in enumerate(order)
        if item[0] == "push" and item[1].get("status") == "done"
    )
    commit_index = next(
        index for index, item in enumerate(order) if item[0] == "commit"
    )
    assert commit_index < terminal_push
    assert order[commit_index][1][1] == "turn-1"
    assert order[commit_index][2]["user_id"] == 7
    assert task["status"] == "done"
    assert task["result"] == "译文"


def test_segment_map_translates_thinking_by_block_id(monkeypatch):
    from lib.translate.runtime import _segments as segments_mod

    calls = []

    def fake_translate(text, _prompt, **kwargs):
        calls.append(text)
        return f"译文:{text}", {}

    monkeypatch.setattr(segments_mod, "_translate_freetext", fake_translate)
    monkeypatch.setattr(
        segments_mod, "is_predominantly_chinese", lambda _text: False,
    )

    segs = [
        {"type": "thinking", "blockId": "thinking:llm-1", "llmRound": 1,
         "text": "reason one"},
        {"type": "text", "blockId": "text:llm-1", "llmRound": 1,
         "deliverable": False, "text": "narration one"},
        {"type": "tool_use", "blockId": "tool:call-1", "llmRound": 1},
        {"type": "thinking", "blockId": "thinking:terminal", "terminal": True,
         "text": "final reason"},
        {"type": "text", "blockId": "text:terminal", "deliverable": True,
         "terminal": True, "text": "the answer"},
        {"type": "thinking", "blockId": "thinking:llm-2", "llmRound": 2,
         "text": "already done", "translatedText": "已有译文"},
    ]
    seg_map = segments_mod._translate_segments_to_map(
        segs, "prompt", "English", "Chinese",
    )

    assert seg_map == {
        "thinking:llm-1": "译文:reason one",
        1: "译文:narration one",
        "thinking:terminal": "译文:final reason",
    }
    # Narration and reasoning of one round never collide on the round key,
    # deliverable prose stays on the translatedContent channel, and a
    # stamped segment is never re-translated.
    assert calls == ["reason one", "narration one", "final reason"]

def test_bound_worker_fails_closed_without_owner():
    from lib.translate.runtime._worker import _do_translate

    task = _register_task("worker-ownerless")
    _do_translate(
        "worker-ownerless", "answer", "Chinese", "English",
        "conversation-1", "turn-1", "translatedContent",
        user_id=None,
    )
    assert task["status"] == "error"
    assert "userId" in str(task["error"]) or "user_id" in str(task["error"])


def test_bound_worker_restores_plan_delimiter_in_running_partial(monkeypatch):
    import lib.translate.runtime._worker as worker

    _register_task("worker-plan-partial", userId=7)
    pushed = []

    def fake_translate(text, _prompt, **kwargs):
        placeholders = [token for token in text.split() if "NT_" in token]
        assert len(placeholders) == 2
        kwargs["progress_cb"](
            f"准备。\n{placeholders[0]}\n## 步骤\n- 正在修改"
        )
        return (
            f"准备。\n{placeholders[0]}\n## 步骤\n- 修改解析器\n"
            f"{placeholders[1]}",
            {"_dispatch": {"model": "translator"}},
        )

    monkeypatch.setattr(worker, "_translate_freetext", fake_translate)
    monkeypatch.setattr(
        worker, "_build_segment_translation_map", lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        worker, "commit_translation_to_turn", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "lib.agent_core.push.push_event",
        lambda _channel, _task_id, frame, *, user_id: pushed.append(frame),
    )

    worker._do_translate(
        "worker-plan-partial",
        "Ready.\n<proposed_plan>\n## Steps\n- change parser\n</proposed_plan>",
        "Chinese",
        "English",
        "conversation-1",
        "turn-1",
        "translatedContent",
        user_id=7,
    )

    partials = [
        frame["partial"] for frame in pushed
        if frame.get("status") == "running" and frame.get("partial")
    ]
    assert any(
        partial.endswith("<proposed_plan>\n## 步骤\n- 正在修改")
        for partial in partials
    )
