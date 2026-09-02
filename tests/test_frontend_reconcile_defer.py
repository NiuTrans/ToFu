"""Settled lifecycle classification remains exclusively backend-owned."""

from __future__ import annotations

import re

import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit


_RETIRED_CLASSIFIERS = (
    "_classifyGhostTail",
    "_isBuriedEmptyGhost",
    "_sweepBuriedGhostAssistants",
    "assistantTailIsPriorTurn",
)


def _assert_no_frontend_lifecycle_classifier(source: str) -> None:
    for symbol in _RETIRED_CLASSIFIERS:
        assert symbol not in source
    assert not re.search(
        r"if\s*\(!conv\._turnSnapshotRequired\s*&&\s*!activeStreams\.has\(conv\.id\)",
        source,
    )


def test_startup_defers_turn_hydration_to_the_selected_conversation():
    source = runtime_section("main/main_init_tasks.js")
    lifecycle = runtime_section("main/main_conv_lifecycle.js")
    _assert_no_frontend_lifecycle_classifier(source)
    assert "ConversationTurnStore" in source
    assert ".hydrateConversation(conversation)" not in source
    assert "await hydrateConversationRuntime(c.id)" in lifecycle


def test_catalog_settings_do_not_project_a_dead_reconcile_marker():
    source = runtime_section("core/conv_apply_settings.js")
    assert "_reconciledAt" not in source


def test_resurrected_classifier_is_detected():
    source = runtime_section("main/main_init_tasks.js")
    resurrected = source + "\n_classifyGhostTail(conv);\n"
    with pytest.raises(AssertionError):
        _assert_no_frontend_lifecycle_classifier(resurrected)
