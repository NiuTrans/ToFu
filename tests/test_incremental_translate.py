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


def test_operation_buffer_can_prioritize_terminal_over_all_previews():
    from lib.translate._operation_buffer import IncrementalOperationBuffer

    buffer = IncrementalOperationBuffer(capacity=4)
    assert buffer.put_segment(1, 'one') == 0
    assert buffer.put_segment(2, 'two') == 0
    assert buffer.put_terminal(
        ('finalize', 'authoritative'), replace=True) == 2

    assert buffer.get(0) == ('finalize', 'authoritative')
    assert buffer.snapshot()['droppedSegments'] == 2


def test_operation_buffer_preserves_explicit_terminal_reasoning():
    from lib.translate._operation_buffer import IncrementalOperationBuffer

    buffer = IncrementalOperationBuffer(capacity=4)
    assert buffer.put_segment(1, 'old narration') == 0
    assert buffer.put_segment('thinking:terminal', 'final reasoning') == 0
    assert buffer.put_terminal(
        ('finalize', 'authoritative'),
        replace=True,
        preserve_segment_keys=frozenset({'thinking:terminal'}),
    ) == 1

    assert buffer.get(0) == (
        'segment', 'thinking:terminal', 'final reasoning')
    assert buffer.get(0) == ('finalize', 'authoritative')


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
        "_attemptId": "inc-attempt",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "_assistantMsgId": "render-message",
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }

    pushed = []
    previews_done = threading.Event()
    preview_calls = []
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        assert overall_deadline in (None, incremental._PREVIEW_DEADLINE_SECONDS)
        if overall_deadline is not None:
            preview_calls.append(text)
            if len(preview_calls) == 2:
                previews_done.set()
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
    assert previews_done.wait(2)
    assert incremental.finalize_incremental(task, "Final answer")
    assert done.wait(2)

    assert committed["args"][:4] == (
        "conversation-1", "turn-1", "translatedContent", "译:Final answer")
    assert committed["kwargs"]["user_id"] == 9
    assert committed["kwargs"]["segment_translations"] == {
        "text:attempt-inc-attempt:llm-1": "译:Narration",
        "text:attempt-inc-attempt:llm-2": "译:Final answer",
    }
    assert any(
        frame.get("status") == "running" and frame.get("partial", "").startswith("流:")
        for frame in pushed
    )


def test_incremental_mixed_segment_does_not_skip_on_dominant_language(
        monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        "id": "incremental-mixed",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }
    accumulator = incremental._Accumulator(task)
    source = (
        "中文说明。" * 40
        + "\nThe final assistant paragraph remains untranslated. " * 20
    )
    calls = []

    monkeypatch.setattr(
        "lib.text_lang.detect_language",
        lambda *_a, **_k: type("Result", (), {"code": "zh"})(),
    )
    monkeypatch.setattr(
        "lib.translate.engine._translate_freetext",
        lambda text, *_a, **_k: (
            calls.append(text) or "完整译文",
            {"_dispatch": {"model": "translator"}},
        ),
    )

    translated, model = accumulator._translate(source)

    assert calls == [source]
    assert translated == "完整译文"
    assert model == "translator"


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
        "_attemptId": "thinking-attempt",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }
    pushed = []
    previews_done = threading.Event()
    preview_calls = []
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        if overall_deadline is not None:
            preview_calls.append(text)
            if len(preview_calls) == 2:
                previews_done.set()
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
    assert previews_done.wait(2)
    assert incremental.finalize_incremental(task, "Final answer")
    assert done.wait(2)

    assert committed["kwargs"]["segment_translations"] == {
        "thinking:attempt-thinking-attempt:llm-0": "译:Let me think",
        "text:attempt-thinking-attempt:llm-0": "译:Narration",
    }
    partials = [
        frame.get("partial") for frame in pushed
        if frame.get("status") == "running"
    ]
    assert partials
    assert all("译:Let me think" not in (partial or "")
               for partial in partials)
    assert any(
        (frame.get("partialByRound") or {}).get(
            "thinking:attempt-thinking-attempt:llm-0")
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


def test_incremental_preview_budget_does_not_limit_terminal_translation(
        monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        "id": "incremental-budget",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }
    accumulator = incremental._Accumulator(task)
    calls = []
    monkeypatch.setattr(incremental, '_MAX_PREVIEW_SEGMENTS', 2)
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        calls.append((text, overall_deadline))
        return f'译:{text}', 'translator'

    monkeypatch.setattr(incremental._Accumulator, '_translate', fake_translate)
    for key in range(5):
        accumulator._process_preview_segment(key, f'preview-{key}')
    assert accumulator._translated_deliverable('final answer') == \
        '译:final answer'

    assert calls == [
        ('preview-0', incremental._PREVIEW_DEADLINE_SECONDS),
        ('preview-1', incremental._PREVIEW_DEADLINE_SECONDS),
        ('final answer', None),
    ]


def test_short_narration_preview_spends_no_model_call_but_terminal_does(
        monkeypatch):
    """Tiny tool-prelude prose is reconstructible UI enrichment, not a reason
    to spend one provider request. The authoritative terminal deliverable must
    still translate with the ordinary (non-preview) rate-limit budget."""
    import lib.translate.incremental as incremental

    accumulator = incremental._Accumulator({
        'id': 'incremental-short-preview',
        'convId': 'conversation-1',
        '_turnId': 'turn-1',
        '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    })
    calls = []
    monkeypatch.setattr(
        incremental, '_PREVIEW_MIN_NARRATION_CHARS', 256)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        calls.append((text, overall_deadline, max_429_attempts,
                      defer_on_shared_contention))
        return f'译:{text}', 'translator'

    monkeypatch.setattr(incremental._Accumulator, '_translate', fake_translate)
    short = 'I will inspect the failing test first.'
    accumulator._process_preview_segment('text:attempt-a:llm-3', short)
    assert calls == []
    assert accumulator._preview_segments_started == 0

    assert accumulator._translated_deliverable(short) == f'译:{short}'
    assert calls == [(short, None, None, False)]


def test_preview_and_terminal_reasoning_use_distinct_429_budgets(monkeypatch):
    """Optional previews yield after one rejected upstream attempt; terminal
    reasoning stays outside both the preview count and its tight 429 budget."""
    import lib.translate.incremental as incremental

    accumulator = incremental._Accumulator({
        'id': 'incremental-preview-429-budget',
        'convId': 'conversation-1',
        '_turnId': 'turn-1',
        '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    })
    calls = []
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)
    monkeypatch.setattr(incremental, '_PREVIEW_MAX_429_ATTEMPTS', 1)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        calls.append((text, overall_deadline, max_429_attempts,
                      defer_on_shared_contention))
        return f'译:{text}', 'translator'

    monkeypatch.setattr(incremental._Accumulator, '_translate', fake_translate)
    accumulator._process_preview_segment(
        'text:attempt-a:llm-1', 'A substantive narration segment.')
    accumulator._process_preview_segment(
        'thinking:terminal', 'Terminal reasoning')

    assert calls == [
        ('A substantive narration segment.',
         incremental._PREVIEW_DEADLINE_SECONDS, 1, True),
        ('Terminal reasoning', incremental._PREVIEW_DEADLINE_SECONDS,
         None, False),
    ]
    assert accumulator._preview_segments_started == 1


def test_incremental_threads_contention_policy_to_translation_dispatch(
        monkeypatch):
    import types

    import lib.translate.incremental as incremental

    accumulator = incremental._Accumulator({
        'id': 'incremental-contention-policy',
        'convId': 'conversation-1',
        '_turnId': 'turn-1',
        '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    })
    observed = []
    monkeypatch.setattr(
        'lib.text_lang.detect_language',
        lambda *args, **kwargs: types.SimpleNamespace(code='en'),
    )

    def _translate(*args, **kwargs):
        observed.append(kwargs['defer_on_shared_contention'])
        return '译文', {'_dispatch': {'model': 'translator'}}

    monkeypatch.setattr('lib.translate.engine._translate_freetext', _translate)

    assert accumulator._translate(
        'optional preview', defer_on_shared_contention=True,
    ) == ('译文', 'translator')
    assert accumulator._translate('terminal deliverable') == \
        ('译文', 'translator')
    assert observed == [True, False]


def test_incremental_protected_only_skips_language_probe_and_model(monkeypatch):
    import lib.translate.incremental as incremental

    accumulator = incremental._Accumulator({
        'id': 'incremental-protected-only',
        'convId': 'conversation-1',
        '_turnId': 'turn-1',
        '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    })
    monkeypatch.setattr(
        'lib.text_lang.detect_language',
        lambda *args, **kwargs: pytest.fail(
            'protected-only text used language detection'),
    )
    monkeypatch.setattr(
        'lib.translate.engine._translate_freetext',
        lambda *args, **kwargs: pytest.fail(
            'protected-only text used a model'),
    )

    translated, model = accumulator._translate(
        '<notranslate>DO_NOT_TRANSLATE</notranslate>',
        overall_deadline=incremental._PREVIEW_DEADLINE_SECONDS,
    )

    assert translated == 'DO_NOT_TRANSLATE'
    assert model == 'skipped'


def test_incremental_preview_failure_isolated_from_terminal(monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        "id": "incremental-failure",
        "convId": "conversation-1",
        "_turnId": "turn-1",
        "_userId": 9,
        "config": {"autoTranslate": True, "uiLang": "zh"},
    }
    accumulator = incremental._Accumulator(task)
    calls = []
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)
    monkeypatch.setattr(incremental, '_PREVIEW_MAX_429_ATTEMPTS', 1)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        calls.append((text, overall_deadline, max_429_attempts))
        if overall_deadline is not None and max_429_attempts is not None:
            raise RuntimeError('preview 429 budget exhausted')
        return f'译:{text}', 'translator'

    monkeypatch.setattr(incremental._Accumulator, '_translate', fake_translate)
    accumulator._process_preview_segment(1, 'first')
    accumulator._process_preview_segment(2, 'must be skipped')
    accumulator._process_preview_segment(
        'thinking:terminal', 'terminal reasoning')

    assert accumulator._translated_deliverable('final answer') == \
        '译:final answer'
    assert calls == [
        ('first', incremental._PREVIEW_DEADLINE_SECONDS, 1),
        ('terminal reasoning', incremental._PREVIEW_DEADLINE_SECONDS, None),
        ('final answer', None, None),
    ]


def test_preview_failure_circuit_survives_accumulator_recreation(monkeypatch):
    """Idle thread retirement cannot buy another preview 429 for the Turn."""
    import lib.translate.incremental as incremental

    task = {
        'id': 'incremental-preview-circuit',
        'convId': 'conversation-1',
        '_turnId': 'turn-1',
        '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    calls = []
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        calls.append((text, max_429_attempts))
        if text == 'first preview':
            raise RuntimeError('preview 429 budget exhausted')
        return f'译:{text}', 'translator'

    monkeypatch.setattr(incremental._Accumulator, '_translate', fake_translate)
    first = incremental._Accumulator(task)
    first._process_preview_segment('text:first', 'first preview')
    assert task[incremental._TASK_PREVIEW_STATE][
        incremental._PREVIEW_STATE_DISABLED] is True

    # This is the state after the five-minute idle worker removed itself.
    recreated = incremental._Accumulator(task)
    recreated._process_preview_segment('text:later', 'later preview')
    recreated._process_preview_segment(
        'thinking:terminal', 'terminal reasoning')

    assert calls == [
        ('first preview', incremental._PREVIEW_MAX_429_ATTEMPTS),
        ('terminal reasoning', None),
    ]

    original_accumulator = incremental._Accumulator
    monkeypatch.setattr(
        incremental,
        '_Accumulator',
        lambda _task: pytest.fail(
            'disabled preview recreated an idle worker'),
    )
    monkeypatch.setattr(incremental, '_accumulators', {})
    assert incremental.submit_round_segment(
        task, 3, 'another later preview') is False
    assert incremental._accumulators == {}
    monkeypatch.setattr(incremental, '_Accumulator', original_accumulator)


def test_preview_count_budget_survives_accumulator_recreation(monkeypatch):
    """The configured call ceiling belongs to the Turn, not one idle thread."""
    import lib.translate.incremental as incremental

    task = {
        'id': 'incremental-preview-count',
        'convId': 'conversation-1',
        '_turnId': 'turn-1',
        '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    calls = []
    monkeypatch.setattr(incremental, '_MAX_PREVIEW_SEGMENTS', 2)
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)

    def fake_translate(
            _self, text, progress_cb=None, overall_deadline=None,
            max_429_attempts=None, defer_on_shared_contention=False):
        calls.append((text, overall_deadline))
        return f'译:{text}', 'translator'

    monkeypatch.setattr(incremental._Accumulator, '_translate', fake_translate)
    first = incremental._Accumulator(task)
    first._process_preview_segment('text:one', 'preview one')
    first._process_preview_segment('text:two', 'preview two')

    recreated = incremental._Accumulator(task)
    recreated._process_preview_segment('text:three', 'preview three')
    assert recreated._translated_deliverable('final answer') == \
        '译:final answer'

    assert task[incremental._TASK_PREVIEW_STATE][
        incremental._PREVIEW_STATE_STARTED] == 2
    assert calls == [
        ('preview one', incremental._PREVIEW_DEADLINE_SECONDS),
        ('preview two', incremental._PREVIEW_DEADLINE_SECONDS),
        ('final answer', None),
    ]


def test_terminal_handoff_clears_task_preview_admission_state(monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        'id': 'incremental-preview-cleanup',
        incremental._TASK_PREVIEW_STATE: {
            incremental._PREVIEW_STATE_DISABLED: True,
            incremental._PREVIEW_STATE_STARTED: 7,
            incremental._PREVIEW_STATE_LIMIT_REPORTED: True,
        },
    }

    class FakeAccumulator:
        def finalize(self, content):
            return content == 'final answer'

    monkeypatch.setattr(
        incremental, '_accumulators', {task['id']: FakeAccumulator()})

    assert incremental.finalize_incremental(task, 'final answer') is True
    assert incremental._TASK_PREVIEW_STATE not in task


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
