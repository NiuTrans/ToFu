"""The async worker persists before exposing a terminal result."""

import re

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
        "_schedule_segment_enrichment",
        lambda *args, **kwargs: order.append(("schedule", args, kwargs)),
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
    schedule_index = next(
        index for index, item in enumerate(order) if item[0] == "schedule"
    )
    assert terminal_push < schedule_index
    assert order[commit_index][1][1] == "turn-1"
    assert order[commit_index][2]["user_id"] == 7
    assert task["status"] == "done"
    assert task["result"] == "译文"


def test_segment_enrichment_failure_cannot_overwrite_whole_turn_success(
        monkeypatch):
    import lib.translate.runtime._worker as worker

    task = _register_task("worker-enrichment-failure", userId=7)
    commits = []
    monkeypatch.setattr(
        worker,
        "_translate_freetext",
        lambda *args, **kwargs: (
            "完整译文", {"_dispatch": {"model": "translator"}},
        ),
    )

    def fail_enrichment(*_args, **_kwargs):
        assert commits, "whole-turn output must be durable before enrichment"
        raise RuntimeError("optional segment provider failed")

    monkeypatch.setattr(
        worker, "_build_segment_translation_map", fail_enrichment,
    )
    monkeypatch.setattr(
        worker,
        "commit_translation_to_turn",
        lambda *args, **kwargs: commits.append((args, kwargs)),
    )
    monkeypatch.setattr(
        worker,
        "_schedule_segment_enrichment",
        lambda *args, **kwargs: worker._enrich_committed_turn_segments(
            *args, **kwargs),
    )

    worker._do_translate(
        task["id"],
        "complete answer",
        "Chinese",
        "English",
        "conversation-1",
        "turn-1",
        "translatedContent",
        user_id=7,
    )

    assert len(commits) == 1
    assert commits[0][0][2:4] == ("translatedContent", "完整译文")
    assert task["status"] == "done"
    assert task["result"] == "完整译文"


def test_running_worker_abort_settles_aborted_without_error(monkeypatch):
    from lib.llm import AbortedError
    import lib.translate.runtime._worker as worker

    task = _register_task('worker-running-abort')

    def aborting_translate(*_args, **kwargs):
        assert callable(kwargs['abort_check'])
        task['abort_event'].set()
        raise AbortedError('owner stopped translation')

    monkeypatch.setattr(worker, '_translate_freetext', aborting_translate)
    worker._do_translate(
        task['id'],
        'answer',
        'Chinese',
        'English',
        '',
        '',
        'translatedContent',
        user_id=1,
    )

    assert task['status'] == 'aborted'
    assert task['error'] is None


def test_segment_map_translates_thinking_by_block_id(monkeypatch):
    from lib.translate.runtime import _segments as segments_mod

    calls = []

    def fake_translate(text, _prompt, **kwargs):
        calls.append(text)
        return (
            text.replace("reason one", "译文:reason one")
            .replace("narration one", "译文:narration one")
            .replace("final reason", "译文:final reason"),
            {},
        )

    monkeypatch.setattr(segments_mod, "_translate_freetext", fake_translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, "get", lambda *args, **kwargs: None,
    )
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
        "text:llm-1": "译文:narration one",
        "thinking:terminal": "译文:final reason",
    }
    # Narration and reasoning of one round never collide on the round key,
    # deliverable prose stays on the translatedContent channel, and a
    # stamped segment is never re-translated.
    assert len(calls) == 1
    assert all(
        source in calls[0]
        for source in ("reason one", "narration one", "final reason")
    )


def test_segment_map_does_not_merge_attempt_local_round_numbers(monkeypatch):
    from lib.translate.runtime import _segments as segments_mod

    monkeypatch.setattr(
        segments_mod,
        '_translate_freetext',
        lambda text, _prompt, **_kwargs: (f'译:{text}', {}),
    )
    monkeypatch.setattr(
        segments_mod.translate_cache, 'get', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        segments_mod, 'is_predominantly_chinese', lambda _text: False)
    monkeypatch.setattr(segments_mod, '_SEGMENT_BATCH_MAX_ITEMS', 1)
    segments = [
        {
            'type': 'text',
            'blockId': 'text:attempt-old:llm-0',
            'attemptId': 'old',
            'llmRound': 0,
            'deliverable': False,
            'text': 'old narration',
        },
        {
            'type': 'text',
            'blockId': 'text:attempt-new:llm-0',
            'attemptId': 'new',
            'llmRound': 0,
            'deliverable': False,
            'text': 'new narration',
        },
    ]

    translated = segments_mod._translate_segments_to_map(
        segments, 'prompt', 'English', 'Chinese')

    assert translated == {
        'text:attempt-old:llm-0': '译:old narration',
        'text:attempt-new:llm-0': '译:new narration',
    }


def test_segment_map_batches_32_cache_misses_into_two_calls(monkeypatch):
    from lib.translate.runtime import _segments as segments_mod

    calls = []

    def fake_translate(text, _prompt, **kwargs):
        calls.append(text)
        return text.replace("segment", "译文"), {
            "_dispatch": {"model": "batch-translator"},
        }

    monkeypatch.setattr(segments_mod, "_translate_freetext", fake_translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, "get", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        segments_mod, "is_predominantly_chinese", lambda _text: False,
    )
    segments = [
        {
            "type": "thinking",
            "blockId": f"thinking:llm-{index}",
            "llmRound": index,
            "text": f"segment {index}.",
        }
        for index in range(32)
    ]

    result = segments_mod._translate_segments_to_map(
        segments, "prompt", "English", "Chinese",
    )

    assert len(calls) == 2
    assert len(result) == 32
    assert result["thinking:llm-0"] == "译文 0."
    assert result["thinking:llm-31"] == "译文 31."


def test_segment_batch_marker_damage_falls_back_to_isolated_calls(monkeypatch):
    from lib.translate.runtime import _segments as segments_mod

    calls = []

    def fake_translate(text, _prompt, **kwargs):
        calls.append(text)
        if len(calls) == 1:
            return re.sub(r"⟦NT_\d+⟧", "", text, count=1), {}
        return f"译文:{text}", {}

    monkeypatch.setattr(segments_mod, "_translate_freetext", fake_translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, "get", lambda *args, **kwargs: None,
    )
    removed = []
    monkeypatch.setattr(
        segments_mod.translate_cache,
        "remove",
        lambda text, source, target: removed.append((text, source, target)),
    )
    monkeypatch.setattr(
        segments_mod, "is_predominantly_chinese", lambda _text: False,
    )
    segments = [
        {"type": "thinking", "blockId": "thinking:one", "text": "one."},
        {"type": "thinking", "blockId": "thinking:two", "text": "two."},
    ]

    result = segments_mod._translate_segments_to_map(
        segments, "prompt", "English", "Chinese",
    )

    assert len(calls) == 3
    assert result == {
        "thinking:one": "译文:one.",
        "thinking:two": "译文:two.",
    }
    assert len(removed) == 1
    assert removed[0][1:] == ("English", "Chinese")


def test_segment_batch_abort_never_falls_back_to_more_calls(monkeypatch):
    from lib.llm import AbortedError
    from lib.translate.runtime import _segments as segments_mod

    calls = {'n': 0}
    cancelled = {'value': False}

    def aborting_translate(_text, _prompt, **kwargs):
        calls['n'] += 1
        assert callable(kwargs['abort_check'])
        cancelled['value'] = True
        raise AbortedError('stop segment batch')

    monkeypatch.setattr(segments_mod, '_translate_freetext', aborting_translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'get', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'remove', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod, 'is_predominantly_chinese', lambda _text: False)

    with pytest.raises(AbortedError, match='stop segment batch'):
        segments_mod._translate_segments_to_map(
            [
                {'type': 'thinking', 'blockId': 'thinking:one', 'text': 'one'},
                {'type': 'thinking', 'blockId': 'thinking:two', 'text': 'two'},
            ],
            'prompt',
            'English',
            'Chinese',
            abort_check=lambda: cancelled['value'],
        )

    assert calls['n'] == 1


def test_segment_batch_no_admissible_provider_is_skipped_without_fanout(
        monkeypatch):
    from lib.translate.errors import TranslationNoAdmissibleProvider
    from lib.translate.runtime import _segments as segments_mod

    calls = {'n': 0}

    def unavailable(_text, _prompt, **_kwargs):
        calls['n'] += 1
        raise TranslationNoAdmissibleProvider()

    monkeypatch.setattr(segments_mod, '_translate_freetext', unavailable)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'get', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'remove', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod, 'is_predominantly_chinese', lambda _text: False)

    result = segments_mod._translate_segments_to_map(
        [
            {'type': 'thinking', 'blockId': 'thinking:one', 'text': 'one'},
            {'type': 'thinking', 'blockId': 'thinking:two', 'text': 'two'},
        ],
        'prompt',
        'English',
        'Chinese',
    )

    assert calls['n'] == 1
    assert result == {}


def test_settled_turn_segment_enrichment_has_one_shared_tight_budget(
        monkeypatch):
    from lib.translate.runtime import _segments as segments_mod

    calls = []

    def fake_translate(text, _prompt, **kwargs):
        calls.append(kwargs)
        return text.replace('segment', '译文'), {}

    monkeypatch.setattr(segments_mod, '_translate_freetext', fake_translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'get', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod, 'is_predominantly_chinese', lambda _text: False)
    monkeypatch.setattr(
        segments_mod,
        '_read_turn_segments',
        lambda *_args, **_kwargs: [
            {'type': 'thinking', 'blockId': 'thinking:one',
             'text': 'segment one'},
            {'type': 'thinking', 'blockId': 'thinking:two',
             'text': 'segment two'},
        ],
    )

    result = segments_mod._build_segment_translation_map(
        'conversation',
        'turn',
        'prompt',
        'English',
        'Chinese',
        user_id=7,
    )

    assert len(calls) == 1
    assert result == {
        'thinking:one': '译文 one',
        'thinking:two': '译文 two',
    }
    assert 0 < calls[0]['overall_deadline'] <= (
        segments_mod._SEGMENT_ENRICHMENT_DEADLINE_SECONDS)
    assert calls[0]['max_429_attempts'] == 1
    assert calls[0]['defer_on_shared_contention'] is True


def test_protected_only_segment_uses_zero_translation_calls(monkeypatch):
    from lib.translate.runtime import _segments as segments_mod

    monkeypatch.setattr(
        segments_mod,
        "_translate_freetext",
        lambda *args, **kwargs: pytest.fail("protected-only text used a model"),
    )
    monkeypatch.setattr(
        segments_mod.translate_cache, "get", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        segments_mod, "is_predominantly_chinese", lambda _text: False,
    )

    result = segments_mod._translate_segments_to_map(
        [{
            "type": "thinking",
            "blockId": "thinking:protected",
            "text": "<notranslate>DO_NOT_TRANSLATE</notranslate>",
        }],
        "prompt",
        "English",
        "Chinese",
    )

    assert result == {"thinking:protected": "DO_NOT_TRANSLATE"}


def test_protected_only_whole_turn_uses_zero_translation_calls(monkeypatch):
    import lib.translate.runtime._worker as worker

    task = _register_task("worker-protected-only")
    monkeypatch.setattr(
        worker,
        "_translate_freetext",
        lambda *args, **kwargs: pytest.fail("protected-only turn used a model"),
    )

    worker._do_translate(
        "worker-protected-only",
        "<notranslate>DO_NOT_TRANSLATE</notranslate>",
        "Chinese",
        "English",
        "",
        "",
        "translatedContent",
        user_id=1,
    )

    assert task["status"] == "done"
    assert task["result"] == "DO_NOT_TRANSLATE"
    assert task["model"] == "skipped"

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
        worker, "_schedule_segment_enrichment", lambda *args, **kwargs: None,
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
