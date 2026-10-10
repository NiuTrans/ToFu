"""Desktop egress preference repository ownership and migration contracts."""

from __future__ import annotations

import json

import pytest

from lib.desktop.egress_preferences import EgressAgentPreferenceRepository


pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, initial: dict | None = None) -> None:
        self.row = initial
        self.calls: list[tuple] = []

    def query(self, operation, payload):
        self.calls.append(("query", operation, payload))
        assert operation == "desktop.egress_agent.get"
        return self.row or {
            "present": False, "agent_id": "", "updated_at_ms": 0}

    def command(self, operation, payload, command_id):
        self.calls.append(("command", operation, payload, command_id))
        if operation == "desktop.egress_agent.initialize" and self.row:
            return self.row
        self.row = {
            "present": True,
            "agent_id": payload["agent_id"],
            "updated_at_ms": 1,
        }
        return self.row


def _repository(client: _Client) -> EgressAgentPreferenceRepository:
    return EgressAgentPreferenceRepository(
        23, client_factory=lambda *, write=False: client)


def test_existing_sidecar_marker_never_reads_the_legacy_file(monkeypatch):
    client = _Client({"present": True, "agent_id": "agent-new", "updated_at_ms": 1})
    monkeypatch.setattr(
        "lib.config_dir.config_path",
        lambda _name: pytest.fail("legacy file must not be consulted"),
    )

    assert _repository(client).pinned_agent() == "agent-new"
    assert [call[0] for call in client.calls] == ["query"]


def test_missing_marker_imports_one_owner_then_persists_completion(
        monkeypatch, tmp_path):
    legacy = tmp_path / "oauth_egress_agents.json"
    legacy.write_text(json.dumps({"23": "agent-old", "24": "private"}))
    monkeypatch.setattr("lib.config_dir.config_path", lambda _name: str(legacy))
    client = _Client()

    assert _repository(client).pinned_agent() == "agent-old"
    initialize = client.calls[-1]
    assert initialize[1] == "desktop.egress_agent.initialize"
    assert initialize[2] == {"owner_user_id": 23, "agent_id": "agent-old"}
    assert "private" not in repr(initialize)
    assert client.row["present"] is True


def test_corrupt_legacy_file_is_not_rewritten_and_initializes_empty_marker(
        monkeypatch, tmp_path):
    legacy = tmp_path / "oauth_egress_agents.json"
    legacy.write_text('{"23":')
    monkeypatch.setattr("lib.config_dir.config_path", lambda _name: str(legacy))
    client = _Client()

    assert _repository(client).pinned_agent() == ""
    assert client.row["agent_id"] == ""
    assert legacy.read_text() == '{"23":'


def test_oversized_legacy_file_is_not_loaded(monkeypatch, tmp_path):
    legacy = tmp_path / "oauth_egress_agents.json"
    legacy.write_bytes(b" " * (1024 * 1024 + 1))
    monkeypatch.setattr("lib.config_dir.config_path", lambda _name: str(legacy))
    client = _Client()

    assert _repository(client).pinned_agent() == ""
    assert client.row["present"] is True


def test_explicit_set_is_owner_bound_and_bounded():
    client = _Client()
    repository = _repository(client)

    assert repository.set_pinned_agent("agent-1") == "agent-1"
    assert client.calls[-1][2] == {
        "owner_user_id": 23, "agent_id": "agent-1"}
    with pytest.raises(ValueError, match="at most 128"):
        repository.set_pinned_agent("x" * 129)
