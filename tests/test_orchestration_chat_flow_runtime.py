"""Explicit side-effect ports for the shared orchestration chat runtime."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from lib.orchestration_chat_flow_runtime import (
    OrchestrationChatFlowRuntimePorts,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _noop(*_args, **_kwargs):
    return None


def test_runtime_side_effect_ports_are_complete_and_immutable():
    ports = OrchestrationChatFlowRuntimePorts(
        append_event=_noop,
        persist_task_result=_noop,
        notify_terminal=_noop,
        stamp_terminal=_noop,
        store_turns=_noop,
        sync_turns=_noop,
        complete_autopilot=_noop,
    )

    assert ports.append_event is _noop
    assert ports.complete_autopilot is _noop
    with pytest.raises(FrozenInstanceError):
        ports.append_event = lambda: None  # type: ignore[misc]


def test_execution_core_consumes_ports_not_task_package_internals():
    source = (
        ROOT / 'lib/orchestration_chat_flow_runtime.py').read_text()
    execution = source[source.index(
        'def execute_orchestration_chat_flow_task('):]

    assert 'OrchestrationChatFlowRuntimePorts.defaults()' in execution
    assert 'from lib.tasks_pkg' not in execution
    for name in (
        'append_event', 'persist_task_result', 'notify_terminal',
        'stamp_terminal', 'store_turns', 'sync_turns', 'complete_autopilot',
    ):
        assert f'ports.{name}' in execution
