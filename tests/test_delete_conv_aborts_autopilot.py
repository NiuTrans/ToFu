"""Conversation deletion coordinates live work with the storage authority.

The HTTP boundary first stops the owner's in-memory executors, then issues one
owner-scoped ``conversation.delete`` lifecycle command.
"""

from __future__ import annotations

import inspect

import pytest
from quart import Quart


pytestmark = pytest.mark.unit


def _app() -> Quart:
    if "PROVIDE_AUTOMATIC_OPTIONS" not in Quart.default_config:
        Quart.default_config = {
            **Quart.default_config,
            "PROVIDE_AUTOMATIC_OPTIONS": True,
        }
    return Quart(__name__)


@pytest.mark.anyio
async def test_delete_stops_live_work_before_owner_scoped_storage_command(
    monkeypatch,
):
    import lib.tasks_pkg.manager as task_manager
    import routes.conversations as conversations

    events: list[tuple] = []

    def abort(conversation_id, **kwargs):
        events.append(("abort", conversation_id, kwargs))

    async def command(operation, conversation_id, payload):
        events.append(("command", operation, conversation_id, payload))
        return {"deleted": True}

    monkeypatch.setattr(task_manager, "abort_running_tasks_for_conv", abort)
    monkeypatch.setattr(conversations, "_command", command)
    monkeypatch.setattr(conversations, "_owner_id", lambda: 73)
    monkeypatch.setattr(
        conversations,
        "_notify_conv_changed",
        lambda *args, **kwargs: events.append(("notify", args, kwargs)),
    )

    handler = inspect.unwrap(conversations.delete_conv)
    async with _app().test_request_context(
        "/api/v1/conversations/conv-delete", method="DELETE"
    ):
        response = await handler("conv-delete")

    _response, status = response if isinstance(response, tuple) else (response, 200)
    assert status == 200
    assert events == [
        (
            "abort",
            "conv-delete",
            {"user_id": 73, "reason": "conversation_deleted"},
        ),
        (
            "command",
            "conversation.delete",
            "conv-delete",
            {"conv_id": "conv-delete", "user_id": 73},
        ),
        (
            "notify",
            ("conv-delete",),
            {"deleted": True, "rev": None, "user_id": 73},
        ),
    ]


@pytest.mark.anyio
async def test_delete_not_found_does_not_publish_change(monkeypatch):
    import lib.tasks_pkg.manager as task_manager
    import routes.conversations as conversations

    monkeypatch.setattr(
        task_manager, "abort_running_tasks_for_conv", lambda _cid, **_kwargs: 0
    )

    async def missing(_operation, _conversation_id, _payload):
        return {"deleted": False}

    monkeypatch.setattr(conversations, "_command", missing)
    monkeypatch.setattr(conversations, "_owner_id", lambda: 73)
    monkeypatch.setattr(
        conversations,
        "_notify_conv_changed",
        lambda *_args, **_kwargs: pytest.fail("missing delete was published"),
    )

    handler = inspect.unwrap(conversations.delete_conv)
    async with _app().test_request_context(
        "/api/v1/conversations/missing", method="DELETE"
    ):
        response, status = await handler("missing")

    assert status == 404
    assert response.status_code == 200  # Quart tuple carries the HTTP status.


@pytest.mark.anyio
async def test_delete_never_removes_storage_while_abort_fails(monkeypatch):
    import lib.tasks_pkg.manager as task_manager
    import routes.conversations as conversations

    def abort_failure(_conversation_id, **_kwargs):
        raise RuntimeError("executor registry unavailable")

    async def forbidden_command(*_args, **_kwargs):
        pytest.fail("storage delete ran while live-work shutdown was uncertain")

    monkeypatch.setattr(
        task_manager, "abort_running_tasks_for_conv", abort_failure
    )
    monkeypatch.setattr(conversations, "_command", forbidden_command)
    monkeypatch.setattr(conversations, "_owner_id", lambda: 73)

    handler = inspect.unwrap(conversations.delete_conv)
    async with _app().test_request_context(
        "/api/v1/conversations/conv-live", method="DELETE"
    ):
        with pytest.raises(RuntimeError, match="registry unavailable"):
            await handler("conv-live")
