"""Bounded ownership and teardown contracts for conversation-sync streams."""

from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.unit


def test_new_generation_supersedes_old_stream_and_wakes_its_waiter():
    from lib.conversation_sync.broker import ConversationWakeBroker

    async def exercise():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        released: list[str] = []
        first = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=1,
        )
        first.add_close_callback(lambda: released.append("first"))
        waiting = asyncio.create_task(first.wait(30))
        await asyncio.sleep(0)

        second = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=2,
        )

        assert await asyncio.wait_for(waiting, timeout=0.2) is False
        assert first.closed is True
        assert first.close_reason == "superseded"
        assert released == ["first"]
        assert broker.snapshot() == {
            "active": 1,
            "scopes": 1,
            "principals": 1,
            "owned": 1,
            "legacy": 0,
            "ownerHistory": 1,
            "ownerHistoryCapacity": 16,
        }
        reconnected = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=2,
        )
        assert second.closed is True
        assert second.close_reason == "superseded"
        assert reconnected.closed is False
        reconnected.close()

    asyncio.run(exercise())


def test_stale_generation_cannot_displace_current_stream():
    from lib.conversation_sync.broker import ConversationWakeBroker

    async def exercise():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        current = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=8,
        )
        stale = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=7,
        )

        assert stale.closed is True
        assert stale.close_reason == "stale_generation"
        assert current.closed is False
        assert broker.snapshot()["active"] == 1
        current.close()

        # The bounded generation tombstone still rejects a delayed reconnect
        # after the current stream has cleanly left the active registry.
        delayed = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=6,
        )
        assert delayed.closed is True
        assert broker.snapshot()["active"] == 0

    asyncio.run(exercise())


def test_capacity_eviction_releases_exact_oldest_local_stream():
    from lib.conversation_sync.broker import ConversationWakeBroker

    async def exercise():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        released: list[str] = []
        oldest = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=1,
        )
        oldest.add_close_callback(lambda: released.append("oldest"))
        newest = broker.subscribe(
            7,
            "conv-b",
            principal_key="owner:7",
            stream_client_id="page-b",
            stream_generation=1,
        )
        newest.add_close_callback(lambda: released.append("newest"))

        victim = broker.evict_oldest("owner:7", exclude=newest)

        assert victim is oldest
        assert oldest.close_reason == "capacity_evicted"
        assert newest.closed is False
        assert released == ["oldest"]
        assert broker.snapshot()["active"] == 1

        automatic_reconnect = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=1,
        )
        assert automatic_reconnect.closed is True
        assert automatic_reconnect.close_reason == "stale_generation"

        explicit_recovery = broker.subscribe(
            7,
            "conv-a",
            principal_key="owner:7",
            stream_client_id="page-a",
            stream_generation=3,
        )
        assert explicit_recovery.closed is False
        explicit_recovery.close()
        newest.close()
        assert released == ["oldest", "newest"]

    asyncio.run(exercise())


def test_close_callback_bound_after_close_still_runs_once():
    from lib.conversation_sync.broker import ConversationWakeBroker

    async def exercise():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        subscription = broker.subscribe(7, "conv-a")
        assert subscription.close("test") is True
        assert subscription.close("again") is False
        released: list[bool] = []
        subscription.add_close_callback(lambda: released.append(True))
        assert released == [True]

    asyncio.run(exercise())


def test_inactive_owner_generation_history_has_a_global_hard_bound():
    from lib.conversation_sync.broker import ConversationWakeBroker

    async def exercise():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        for index in range(24):
            subscription = broker.subscribe(
                7,
                f"conv-{index}",
                principal_key="owner:7",
                stream_client_id="page-a",
                stream_generation=2,
            )
            subscription.close()

        snapshot = broker.snapshot()
        assert snapshot["active"] == 0
        assert snapshot["ownerHistory"] == 16
        assert snapshot["ownerHistoryCapacity"] == 16

    asyncio.run(exercise())


def test_unconsumed_response_deadline_releases_subscription_and_slot():
    from lib.conversation_sync.broker import ConversationWakeBroker

    async def exercise():
        broker = ConversationWakeBroker(owner_history_capacity=16)
        released: list[bool] = []
        subscription = broker.subscribe(7, "conv-a")
        subscription.add_close_callback(lambda: released.append(True))
        subscription.arm_body_start_deadline(0.01)

        await asyncio.sleep(0.03)

        assert subscription.closed is True
        assert subscription.close_reason == "body_start_timeout"
        assert released == [True]
        assert broker.snapshot()["active"] == 0

    asyncio.run(exercise())


def test_stream_admission_metrics_have_only_bounded_decision_labels():
    from lib.observability import (
        prometheus_lines,
        record_stream_admission,
        reset_for_tests,
    )

    reset_for_tests()
    record_stream_admission("conversation-sync", "superseded")
    record_stream_admission("conversation-sync", "unbounded-client-value")
    rendered = "\n".join(prometheus_lines())

    assert (
        'tofu_stream_admission_total{channel="conversation-sync",'
        'outcome="superseded"} 1.0'
    ) in rendered
    assert (
        'tofu_stream_admission_total{channel="conversation-sync",'
        'outcome="capacity"} 1.0'
    ) in rendered
    assert "unbounded-client-value" not in rendered
