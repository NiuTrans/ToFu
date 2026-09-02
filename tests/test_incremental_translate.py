"""Incremental translation carries stable turn identity to one final merge."""

import threading

import pytest


pytestmark = pytest.mark.unit


def test_operation_buffer_evicts_previews_but_never_terminal_handoff():
    from lib.translate._operation_buffer import IncrementalOperationBuffer

    buffer = IncrementalOperationBuffer(capacity=2)
    assert buffer.put_segment(1, 'one') == 0
    assert buffer.put_segment(2, 'two') == 0
    assert buffer.put_segment(3, 'three') == 1
    assert buffer.put_terminal(('finalize', 'authoritative')) == 1

    assert buffer.get(0) == ('segment', 3, 'three')
    assert buffer.get(0) == ('finalize', 'authoritative')
    assert buffer.snapshot() == {
        'capacity': 2,
        'depth': 0,
        'peakDepth': 2,
        'droppedSegments': 2,
        'terminalQueued': True,
    }


def test_incremental_accumulator_registry_rejects_new_unique_task_at_capacity(
        monkeypatch):
    import lib.translate.incremental as incremental

    class FakeAccumulator:
        def __init__(self, task):
            self.task_id = task['id']

        def start(self):
            return None

        def submit(self, _round_number, _text):
            return True

    monkeypatch.setattr(incremental, '_Accumulator', FakeAccumulator)
    monkeypatch.setattr(incremental, '_MAX_ACTIVE_ACCUMULATORS', 1)
    monkeypatch.setattr(incremental, '_accumulators', {})
    base = {
        'convId': 'conversation-capacity',
        '_turnId': 'turn-capacity',
        '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    first = {**base, 'id': 'incremental-first'}
    second = {**base, 'id': 'incremental-overflow'}

    assert incremental.submit_round_segment(first, 1, 'first') is True
    assert incremental.submit_round_segment(second, 1, 'second') is False
    assert first['_incremental_translate_active'] is True
    assert '_incremental_translate_active' not in second
    assert list(incremental._accumulators) == ['incremental-first']


def test_incremental_finalize_commits_cached_rounds(monkeypatch):
    import lib.translate.incremental as incremental

    committed = {}
    done = threading.Event()
    task = {
        "id": "incremental-task",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "_assistantMsgId": "render-message",
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }

    pushed = []

    def fake_translate(_self, text, progress_cb=None):
        if progress_cb is not None:
            progress_cb(f"流:{text[:4]}")
        return f"译:{text}", "translator"

    monkeypatch.setattr(incremental._Accumulator, "_translate", fake_translate)

    def capture(*args, **kwargs):
        committed["args"] = args
        committed["kwargs"] = kwargs
        done.set()

    monkeypatch.setattr(
        "lib.translate.commit.commit_translation_to_turn", capture)
    monkeypatch.setattr(
        "lib.agent_core.push.push_event",
        lambda _channel, _task_id, payload, **_kwargs: pushed.append(payload),
    )

    assert incremental.submit_round_segment(task, 1, "Narration")
    assert incremental.submit_round_segment(task, 2, "Final answer")
    assert incremental.finalize_incremental(task, "Final answer")
    assert done.wait(2)

    assert committed["args"][:4] == (
        "conversation-1", "turn-1", "translatedContent", "译:Final answer")
    assert committed["kwargs"]["user_id"] == 9
    assert committed["kwargs"]["segment_translations"] == {
        1: "译:Narration", 2: "译:Final answer",
    }
    assert any(
        frame.get("status") == "running" and frame.get("partial", "").startswith("流:")
        for frame in pushed
    )


def test_incremental_commits_thinking_segments_keyed_by_block_id(monkeypatch):
    """Closed reasoning rounds ride the same accumulator as narration prose,
    keyed by their segment blockId so the terminal commit pins them onto the
    thinking segments (commit._stamp_segment_translations resolves
    ``thinking:`` keys by blockId). The joined bubble preview must never dump
    reasoning translations into it."""
    import lib.translate.incremental as incremental

    committed = {}
    done = threading.Event()
    task = {
        "id": "incremental-thinking",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }
    pushed = []

    def fake_translate(_self, text, progress_cb=None):
        return f"译:{text}", "translator"

    monkeypatch.setattr(incremental._Accumulator, "_translate", fake_translate)

    def capture(*args, **kwargs):
        committed["args"] = args
        committed["kwargs"] = kwargs
        done.set()

    monkeypatch.setattr(
        "lib.translate.commit.commit_translation_to_turn", capture)
    monkeypatch.setattr(
        "lib.agent_core.push.push_event",
        lambda _channel, _task_id, payload, **_kwargs: pushed.append(payload),
    )

    assert incremental.submit_thinking_segment(
        task, "thinking:llm-0", "Let me think")
    assert incremental.submit_round_segment(task, 0, "Narration")
    assert incremental.finalize_incremental(task, "Final answer")
    assert done.wait(2)

    assert committed["kwargs"]["segment_translations"] == {
        "thinking:llm-0": "译:Let me think",
        0: "译:Narration",
    }
    partials = [
        frame.get("partial") for frame in pushed
        if frame.get("status") == "running"
    ]
    assert partials
    assert all("译:Let me think" not in (partial or "")
               for partial in partials)
    assert any(
        (frame.get("partialByRound") or {}).get("thinking:llm-0")
        == "译:Let me think"
        for frame in pushed
    )


def test_incremental_thinking_oversize_defers_to_retro(monkeypatch):
    """Pinning is enrich-only, so an oversize reasoning block must NOT be
    truncated into a permanent partial translation — it defers to the
    retro/on-open path (no accumulator is created)."""
    import lib.translate.incremental as incremental

    monkeypatch.setattr(incremental, "_accumulators", {})
    task = {
        "id": "incremental-oversize",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }
    big = "x" * (incremental._SEGMENT_MAX_CHARS + 10)
    assert incremental.submit_thinking_segment(
        task, "thinking:llm-0", big) is False
    assert incremental._accumulators == {}


def test_terminal_coordinator_submits_thinking_before_finalize(monkeypatch):
    """The terminal round's reasoning enters the accumulator queue BEFORE the
    finalize handoff, so the worker drains it first and the terminal commit
    carries the ``thinking:terminal`` translation."""
    import types

    import lib.translate.terminal as terminal

    calls = []
    task = {
        "id": "task-terminal-thinking",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "thinking": "terminal reasoning",
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }
    monkeypatch.setattr(
        "lib.turn_lifecycle.get_turn",
        lambda conv_id, turn_id, *, user_id: {
            "status": "completed",
            "projection": {"content": "final answer"},
        },
    )
    monkeypatch.setattr(
        "lib.translate.incremental.submit_thinking_segment",
        lambda _task, key, text: calls.append(("thinking", key)) or True,
    )
    monkeypatch.setattr(
        "lib.translate.incremental.finalize_incremental",
        lambda _task, content: calls.append(("finalize", content)) or True,
    )
    monkeypatch.setattr(
        "lib.translate.incremental.cancel_incremental", lambda _task: True)
    monkeypatch.setattr(
        "lib.text_lang.detect_language",
        lambda text, force_fasttext=False: types.SimpleNamespace(code="en"),
    )

    terminal._translate_settled_turns(task)

    assert calls == [
        ("thinking", "thinking:terminal"),
        ("finalize", "final answer"),
    ]

def test_incremental_requires_turn_identity():
    import lib.translate.incremental as incremental

    task = {
        "id": "no-turn",
        "convId": "conversation-1",
        "config": {"autoTranslate": True},
    }
    assert incremental.submit_round_segment(task, 1, "Narration") is False


def test_plan_protocol_tags_are_visible_in_streaming_translation_partials():
    from lib.translate.notranslate import (
        _extract_notranslate_blocks,
        _reattach_notranslate_blocks,
        _reattach_notranslate_blocks_partial,
    )

    source = "Ready.\n<proposed_plan>\n## Steps\n- change parser\n</proposed_plan>"
    protected, blocks = _extract_notranslate_blocks(source)

    assert "<proposed_plan>" not in protected
    assert "</proposed_plan>" not in protected
    assert [block["content"] for block in blocks] == [
        "<proposed_plan>", "</proposed_plan>",
    ]

    partial = f"准备。\n{blocks[0]['placeholder']}\n## 步骤\n- 修改"
    visible_partial = _reattach_notranslate_blocks_partial(partial, blocks)
    assert visible_partial.endswith("<proposed_plan>\n## 步骤\n- 修改")
    assert "</proposed_plan>" not in visible_partial

    completed = (
        partial + f"\n{blocks[1]['placeholder']}"
    )
    assert _reattach_notranslate_blocks(completed, blocks).endswith(
        "<proposed_plan>\n## 步骤\n- 修改\n</proposed_plan>"
    )

    # Missing protocol placeholders must not be appended as an invented,
    # empty envelope after translated prose. Presentation will retain the
    # authoritative original plan when the delimiter contract is incomplete.
    dropped = _reattach_notranslate_blocks("准备。\n## 步骤\n- 修改", blocks)
    assert "<proposed_plan>" not in dropped
    assert "</proposed_plan>" not in dropped
