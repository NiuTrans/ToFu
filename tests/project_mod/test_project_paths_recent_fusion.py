"""Project selection fuses optional recent history after successful validation."""

from __future__ import annotations

import asyncio

import pytest
from tests.support.envelope import v1_get_json


pytestmark = pytest.mark.unit


def _run(awaitable):
    return asyncio.run(awaitable)


def test_project_paths_touches_one_owner_scoped_batch_after_success(
    flask_app,
    monkeypatch,
):
    import lib.project_mod as project_mod
    import routes.api_v1.project as project_routes

    calls = []

    def set_paths(paths, readonly_paths=None):
        calls.append(("set", list(paths), list(readonly_paths or [])))
        return {
            "path": "/workspace/a",
            "extraRoots": [{"path": "/workspace/b"}],
        }

    def touch_recent(paths, *, user_id):
        calls.append(("recent", list(paths), user_id))
        return len(paths)

    monkeypatch.setattr(project_mod, "set_project_paths", set_paths)
    monkeypatch.setattr(project_mod, "save_recent_projects", touch_recent)
    monkeypatch.setattr(project_routes, "_request_user_id", lambda: 41)

    async def scenario():
        response = await flask_app.test_client().put(
            "/api/v4/project/paths",
            json={
                "paths": ["/workspace/a", "/workspace/b"],
                "readOnlyPaths": ["/workspace/b"],
                "recentPaths": ["/workspace/a", "/workspace/b"],
            },
        )
        return response.status_code, await v1_get_json(response)

    status, body = _run(scenario())
    assert status == 200
    assert body["ok"] is True
    assert calls == [
        ("set", ["/workspace/a", "/workspace/b"], ["/workspace/b"]),
        ("recent", ["/workspace/a", "/workspace/b"], 41),
    ]


def test_invalid_recent_subset_fails_before_project_mutation(
    flask_app,
    monkeypatch,
):
    import lib.project_mod as project_mod

    calls = []
    monkeypatch.setattr(
        project_mod,
        "set_project_paths",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    async def scenario():
        response = await flask_app.test_client().put(
            "/api/v4/project/paths",
            json={
                "paths": ["/workspace/a"],
                "recentPaths": ["/workspace/not-selected"],
            },
        )
        return response.status_code, await v1_get_json(response)

    status, body = _run(scenario())
    assert status == 400
    # v4 problem documents carry no top-level ``field``; the projected error
    # message must still name the offending field.
    assert "recentPaths" in body["error"]["message"]
    assert calls == []


def test_recent_persistence_failure_cannot_rollback_valid_project_selection(
    flask_app,
    monkeypatch,
):
    import lib.project_mod as project_mod

    monkeypatch.setattr(
        project_mod,
        "set_project_paths",
        lambda paths, readonly_paths=None: {"path": paths[0]},
    )
    monkeypatch.setattr(
        project_mod,
        "save_recent_projects",
        lambda paths, *, user_id: (_ for _ in ()).throw(
            RuntimeError("synthetic recent outage")
        ),
    )

    async def scenario():
        response = await flask_app.test_client().put(
            "/api/v4/project/paths",
            json={
                "paths": ["/workspace/a"],
                "recentPaths": ["/workspace/a"],
            },
        )
        return response.status_code, await v1_get_json(response)

    status, body = _run(scenario())
    assert status == 200
    assert body == {"ok": True, "path": "/workspace/a"}


def test_omitted_recent_intent_keeps_reconciliation_side_effect_free(
    flask_app,
    monkeypatch,
):
    import lib.project_mod as project_mod

    calls = []
    monkeypatch.setattr(
        project_mod,
        "set_project_paths",
        lambda paths, readonly_paths=None: calls.append((
            "set", list(paths), list(readonly_paths or []),
        )) or {"path": paths[0]},
    )
    monkeypatch.setattr(
        project_mod,
        "save_recent_projects",
        lambda *args, **kwargs: calls.append(("recent", args, kwargs)),
    )

    async def scenario():
        response = await flask_app.test_client().put(
            "/api/v4/project/paths",
            json={"paths": ["/workspace/a"]},
        )
        return response.status_code, await v1_get_json(response)

    status, body = _run(scenario())
    assert status == 200
    assert body == {"ok": True, "path": "/workspace/a"}
    assert calls == [("set", ["/workspace/a"], [])]
