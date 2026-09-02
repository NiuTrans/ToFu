"""Project attachment and agent execution mode are independent state axes."""

from __future__ import annotations

import re

import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unbalanced function: {signature}")


def test_project_attach_changes_capability_tier_not_agent_strategy():
    project = runtime_section("project.js", scope_prelude=False)
    apply_folders = _function(project, "async function mpApplyFolders() {")

    assert "onProjectAttached" in apply_folders
    for forbidden in (
        "_autoEnableProjectModes",
        "_applyAgentModeUI",
        "_applyAutopilotUI",
        "_applySwarmUI",
    ):
        assert forbidden not in apply_folders


def test_retired_project_auto_enable_owner_cannot_be_reintroduced():
    project = runtime_section("project.js", scope_prelude=False)
    assert "function _autoEnableProjectModes" not in project
    assert "swarmEnabled" not in project
    assert "_applySwarmUI" not in project


def test_new_conversation_resets_the_single_agent_mode_owner():
    main = runtime_section("main.js", scope_prelude=False)
    reset = _function(main, "function _resetToolsToDefaults() {")

    assert "_applyAgentModeUI('standard')" in reset
    assert "_applyAutopilotUI" not in reset
    assert "_applyEndpointUI" not in reset
    assert "_applySwarmUI" not in reset


def test_project_created_shell_remains_local_until_first_turn():
    project = runtime_section("project.js", scope_prelude=False)
    apply_folders = _function(project, "async function mpApplyFolders() {")

    assert re.search(r"\b_localOnly\s*:\s*true\b", apply_folders)
    assert "captureActiveConversationSettings" in apply_folders
