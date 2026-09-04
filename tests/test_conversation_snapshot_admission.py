"""Bounded overlap defense for multi-megabyte conversation snapshots."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.conversation_sync.snapshot_admission import (
    ConversationSnapshotAdmission,
)


pytestmark = pytest.mark.unit


def _enter(
    gate: ConversationSnapshotAdmission,
    *,
    user_id: int = 1,
    conversation_id: str = "conv-a",
    page_id: str = "abc123",
    representation: str = "refs",
):
    return gate.enter(
        user_id=user_id,
        conversation_id=conversation_id,
        page_id=page_id,
        representation=representation,
    )


def test_exact_overlap_is_rejected_but_owner_page_and_view_are_isolated():
    gate = ConversationSnapshotAdmission(max_active=8)
    held = _enter(gate)
    assert held.allowed and held.lease is not None

    duplicate = _enter(gate)
    other_owner = _enter(gate, user_id=2)
    other_page = _enter(gate, page_id="def456")
    other_view = _enter(gate, representation="full")

    assert duplicate.allowed is False
    assert duplicate.reason == "snapshot_in_flight"
    assert all(decision.allowed for decision in (
        other_owner, other_page, other_view,
    ))
    for decision in (held, other_owner, other_page, other_view):
        gate.release(decision.lease)
    assert gate.snapshot()["active"] == 0


def test_concurrent_duplicates_have_one_tracked_winner():
    gate = ConversationSnapshotAdmission(max_active=8)
    held = _enter(gate)
    assert held.lease is not None

    with ThreadPoolExecutor(max_workers=16) as pool:
        decisions = list(pool.map(lambda _index: _enter(gate), range(64)))

    assert all(not decision.allowed for decision in decisions)
    assert gate.snapshot() == {
        "capacity": 8,
        "active": 1,
        "peakActive": 1,
        "rejected": 64,
        "capacityBypassed": 0,
    }
    gate.release(held.lease)


def test_capacity_is_bounded_and_fails_open_without_a_phantom_lease():
    gate = ConversationSnapshotAdmission(max_active=1)
    held = _enter(gate)
    bypass = _enter(gate, conversation_id="conv-b")

    assert bypass.allowed is True
    assert bypass.reason == "capacity_bypass"
    assert bypass.lease is None
    assert gate.snapshot()["active"] == 1
    assert gate.snapshot()["capacityBypassed"] == 1

    gate.release(held.lease)
    assert gate.snapshot()["active"] == 0


def test_stale_or_duplicate_release_cannot_remove_a_new_generation():
    gate = ConversationSnapshotAdmission(max_active=2)
    first = _enter(gate)
    gate.release(first.lease)
    second = _enter(gate)
    assert second.lease is not None

    gate.release(first.lease)
    assert _enter(gate).allowed is False
    gate.release(second.lease)
    assert gate.snapshot()["active"] == 0


def _app(gate, service, monkeypatch):
    from quart import Quart, g, request

    import routes.conversation_sync_v3 as sync_routes
    from lib.api_keys import local_admin_context

    app = Quart(__name__)
    app.config["TESTING"] = True

    @app.before_request
    async def _grant():
        context = local_admin_context()
        context.user_id = request.headers.get("X-Test-User", "1")
        g.auth_ctx = context
        g.rate_decision = None

    monkeypatch.setattr(sync_routes, "snapshot_admission", gate)
    monkeypatch.setattr(sync_routes, "_service", service)
    monkeypatch.setattr(
        sync_routes, "push_withheld_for_conv", lambda _conversation_id: None,
    )
    app.register_blueprint(sync_routes.conversation_sync_v3_bp)
    return app


def test_http_overlap_returns_small_retryable_429_and_finally_releases(
    monkeypatch,
):
    class Service:
        def __init__(self):
            self.calls = 0

        def snapshot(self, conversation_id, user_id, **_kwargs):
            self.calls += 1
            return {
                "ok": True,
                "conversationId": conversation_id,
                "owner": user_id,
            }

    gate = ConversationSnapshotAdmission(max_active=4)
    service = Service()
    app = _app(gate, service, monkeypatch)
    held = _enter(gate)

    async def exercise():
        client = app.test_client()
        rejected = await client.get(
            "/api/v3/conversations/conv-a/sync?segmentPayload=refs",
            headers={"X-Request-ID": "abc123-7"},
        )
        # A non-browser request ID bypasses the cooperative page circuit.
        allowed = await client.get(
            "/api/v3/conversations/conv-a/sync?segmentPayload=refs",
            headers={"X-Request-ID": "headless-client"},
        )
        return rejected, allowed

    rejected, allowed = asyncio.run(exercise())
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "1"
    assert rejected.headers["Cache-Control"] == "no-store"
    assert (asyncio.run(rejected.get_json()))["error"] == "snapshot_in_flight"
    assert allowed.status_code == 200
    assert service.calls == 1
    assert gate.snapshot()["active"] == 1

    gate.release(held.lease)
    assert gate.snapshot()["active"] == 0


def test_http_admitted_snapshot_releases_after_storage_failure(monkeypatch):
    from lib.storage.errors import StorageError

    class FailingService:
        @staticmethod
        def snapshot(*_args, **_kwargs):
            raise StorageError("database_unavailable", "offline")

    gate = ConversationSnapshotAdmission(max_active=4)
    app = _app(gate, FailingService(), monkeypatch)

    async def exercise():
        return await app.test_client().get(
            "/api/v3/conversations/conv-a/sync?segmentPayload=refs",
            headers={"X-Request-ID": "abc123-8"},
        )

    response = asyncio.run(exercise())
    assert response.status_code == 503
    assert gate.snapshot()["active"] == 0
