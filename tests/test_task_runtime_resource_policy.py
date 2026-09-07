"""Resource-profile contract for in-process task replay retention."""

import pytest

from lib.agent_core.task_runtime_policy import (
    resolve_chat_task_terminal_ttl_seconds,
    resolve_task_runtime_retention_budget,
)


pytestmark = pytest.mark.unit
_MIB = 1024 * 1024


def test_eight_gib_reference_task_runtime_budget_is_finite():
    budget = resolve_task_runtime_retention_budget(
        {
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "128",
            "TOFU_MAX_INFLIGHT_TASKS": "4",
        }
    )

    assert budget.task_capacity == 128
    assert budget.event_capacity == 2_048
    assert budget.replay_byte_capacity == 4 * _MIB
    assert budget.event_max_bytes == 8 * _MIB
    assert budget.replay_hard_capacity == 8 * _MIB


def test_probe_fallback_task_runtime_budget_stays_lean():
    budget = resolve_task_runtime_retention_budget(
        {
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "64",
            "TOFU_MAX_INFLIGHT_TASKS": "1",
        }
    )

    assert budget.task_capacity == 64
    assert budget.event_capacity == 1_024
    assert budget.replay_byte_capacity == 2 * _MIB
    assert budget.event_max_bytes == 4 * _MIB


def test_distributed_task_runtime_budget_is_larger_but_finite():
    budget = resolve_task_runtime_retention_budget(
        {"TOFU_DEPLOYMENT_MODE": "distributed"}
    )

    assert budget.task_capacity == 512
    assert budget.event_capacity == 4_096
    assert budget.replay_byte_capacity == 8 * _MIB
    assert budget.event_max_bytes == 16 * _MIB


def test_task_runtime_overrides_are_hard_clamped():
    budget = resolve_task_runtime_retention_budget(
        {
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "64",
            "TOFU_MAX_INFLIGHT_TASKS": "1",
            "TOFU_TASK_RUNTIME_TASK_CAPACITY": "999999",
            "TOFU_TASK_RUNTIME_EVENT_CAPACITY": "999999",
            "TOFU_TASK_RUNTIME_REPLAY_MAX_MIB": "999999",
            "TOFU_TASK_RUNTIME_EVENT_MAX_MIB": "999999",
        }
    )

    assert budget.task_capacity == 1_024
    assert budget.event_capacity == 8_192
    assert budget.replay_byte_capacity == 16 * _MIB
    assert budget.event_max_bytes == 16 * _MIB


def test_nonpositive_task_runtime_overrides_restore_profile_defaults():
    budget = resolve_task_runtime_retention_budget(
        {
            "TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY": "128",
            "TOFU_MAX_INFLIGHT_TASKS": "4",
            "TOFU_TASK_RUNTIME_TASK_CAPACITY": "0",
            "TOFU_TASK_RUNTIME_EVENT_CAPACITY": "-2",
            "TOFU_TASK_RUNTIME_REPLAY_MAX_MIB": "bad",
            "TOFU_TASK_RUNTIME_EVENT_MAX_MIB": "0",
        }
    )

    assert budget.task_capacity == 128
    assert budget.event_capacity == 2_048
    assert budget.replay_byte_capacity == 4 * _MIB
    assert budget.event_max_bytes == 8 * _MIB


def test_chat_terminal_ttl_uses_launch_profile_and_hard_ceiling():
    assert resolve_chat_task_terminal_ttl_seconds({
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': '600',
    }) == 600
    assert resolve_chat_task_terminal_ttl_seconds({
        'TOFU_DEPLOYMENT_MODE': 'distributed',
    }) == 3600
    assert resolve_chat_task_terminal_ttl_seconds({
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': '999999',
    }) == 86_400
    assert resolve_chat_task_terminal_ttl_seconds({
        'TOFU_CHAT_TASK_TERMINAL_TTL_SECONDS': 'invalid',
    }) == resolve_chat_task_terminal_ttl_seconds({})


def test_runtime_byte_saturation_keeps_one_contiguous_newest_suffix():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime(
        "byte-tail",
        max_tasks=4,
        max_events=100,
        max_event_buffer_bytes=1_024,
        max_event_bytes=2_048,
        push_channel="",
    )
    task = runtime.create(user_id=1, task_id="byte-tail-task")
    for index in range(3):
        runtime.append_event(
            task["id"],
            {"type": "progress", "index": index, "payload": "x" * 600},
        )

    assert [(event["seq"], event["index"]) for event in task["events"]] == [(2, 2)]
    page = runtime.poll(task["id"], 0)
    assert [(event["seq"], event["index"]) for event in page["events"]] == [(2, 2)]
    assert page["next_cursor"] == 3
    assert page["cursor"] == {"requested": 0, "next": 3, "reset": True}
    stats = runtime.retention_stats()
    assert 0 < stats["event_retained_bytes"] <= 1_024


def test_single_large_event_may_occupy_the_replay_window_alone():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime(
        "large-event",
        max_event_buffer_bytes=256,
        max_event_bytes=2_048,
        push_channel="",
    )
    task = runtime.create(user_id=1, task_id="large-event-task")
    runtime.append_event(task["id"], {"type": "progress", "index": 0})
    runtime.append_event(task["id"], {"type": "progress", "index": 1})
    runtime.append_event(
        task["id"],
        {"type": "progress", "index": 2, "payload": "x" * 600},
    )

    assert [(event["seq"], event["index"]) for event in task["events"]] == [(2, 2)]
    stats = runtime.retention_stats()
    assert 256 < stats["event_retained_bytes"] <= 2_048
    assert stats["event_retention_hard_capacity_per_task"] == 2_048


def test_oversized_event_resets_memory_window_without_reusing_cursor():
    from lib.agent_core.task_runtime import TaskRuntime

    persisted_sequences = []
    runtime = TaskRuntime(
        "oversized-event",
        max_event_buffer_bytes=256,
        max_event_bytes=512,
        push_channel="",
    )
    task = runtime.create(user_id=1, task_id="oversized-event-task")
    assert (
        runtime.append_event(
            task["id"],
            {"type": "progress", "payload": "x" * 1_000},
            before_push=persisted_sequences.append,
        )
        == 0
    )

    assert persisted_sequences == [0]
    assert task["events"] == []
    assert task["_eventBaseSeq"] == 1
    assert task["_eventNextSeq"] == 1
    reset = runtime.poll(task["id"], 0)
    assert reset["events"] == []
    assert reset["next_cursor"] == 1
    assert reset["cursor"] == {"requested": 0, "next": 1, "reset": True}

    assert runtime.append_event(task["id"], {"type": "progress", "index": 1}) == 1
    resumed = runtime.poll(task["id"], 1)
    assert [event["seq"] for event in resumed["events"]] == [1]
    assert resumed["cursor"]["reset"] is False


def test_unencodable_event_cannot_leave_unaccounted_retained_state():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime(
        "unencodable-event",
        max_event_buffer_bytes=256,
        max_event_bytes=512,
        push_channel="",
    )
    task = runtime.create(user_id=1, task_id="unencodable-event-task")

    assert (
        runtime.append_event(task["id"], {"type": "progress", "value": object()}) == 0
    )
    assert task["events"] == []
    assert runtime.retention_stats()["event_retained_bytes"] == 0


def test_legacy_event_window_rebuilds_private_byte_accounting_once():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime(
        "legacy-byte-accounting",
        max_event_buffer_bytes=4_096,
        max_event_bytes=4_096,
        push_channel="",
    )
    task = runtime.create(user_id=1, task_id="legacy-byte-accounting-task")
    with task["events_lock"]:
        task["events"] = [
            {"type": "progress", "seq": 40, "payload": "a" * 100},
            {"type": "progress", "seq": 41, "payload": "b" * 100},
        ]
        task["_eventBaseSeq"] = 40
        task["_eventNextSeq"] = 0

    assert runtime.append_event(task["id"], {"type": "progress"}) == 42
    assert [event["seq"] for event in task["events"]] == [40, 41, 42]
    assert len(task["_eventRetainedSizes"]) == 3
    assert task["_eventRetainedBytes"] == sum(task["_eventRetainedSizes"])
