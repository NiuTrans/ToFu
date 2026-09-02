"""Recent-project application code uses an explicit owner repository."""

from __future__ import annotations

import pytest

from lib.project_mod.config import (
    clear_recent_projects,
    get_recent_projects,
    save_recent_project,
)
from lib.project_mod.recent_repository import RecentProjectRepository


pytestmark = pytest.mark.unit


class _Client:
    def __init__(self):
        self.calls = []

    def query(self, operation, payload):
        self.calls.append(("query", operation, payload))
        return [{"path": "/workspace/a", "count": 1, "last_used": 10}]

    def command(self, operation, payload, command_id):
        self.calls.append(("command", operation, payload, command_id))
        if operation == "project.recent.touch":
            return {"path": payload["project_path"], "count": 2, "last_used": 11}
        return {"deleted": 1}


def test_repository_injects_owner_into_every_operation():
    client = _Client()
    repo = RecentProjectRepository(
        19, client_factory=lambda *, write=False: client)

    assert repo.list()[0]["path"] == "/workspace/a"
    assert repo.touch("/workspace/a")["count"] == 2
    assert repo.clear() == 1
    assert all(call[2]["user_id"] == 19 for call in client.calls)


def test_config_boundary_uses_sidecar_when_legacy_mode_is_requested(
    monkeypatch,
):
    client = _Client()
    monkeypatch.setattr(
        "lib.storage.get_storage_client",
        lambda *, write=False: client,
    )

    assert get_recent_projects(user_id=19)[0]["path"] == "/workspace/a"
    save_recent_project("/workspace/a", user_id=19)
    clear_recent_projects(user_id=19)
    assert [call[1] for call in client.calls] == [
        "project.recent.list",
        "project.recent.touch",
        "project.recent.clear",
    ]


@pytest.mark.parametrize("owner_user_id", [None, 0, -1, True, ""])
def test_repository_rejects_missing_or_invalid_owner(owner_user_id):
    with pytest.raises(ValueError, match="recent project owner"):
        RecentProjectRepository(owner_user_id)
