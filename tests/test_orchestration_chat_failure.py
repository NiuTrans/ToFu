"""Canonical failure boundary for Flow-backed chat tasks."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from lib.orchestration_chat_failure import (
    OrchestrationChatFailurePorts,
    finalize_orchestration_chat_flow_exception,
    finalize_unavailable_orchestration_chat_flow,
    unavailable_selected_flow_reference,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_failure_port_is_immutable_and_complete():
    calls = []
    finalizer = lambda *args, **kwargs: calls.append((args, kwargs))
    ports = OrchestrationChatFailurePorts(finalize_error=finalizer)

    assert ports.finalize_error is finalizer
    with pytest.raises(FrozenInstanceError):
        ports.finalize_error = lambda: None  # type: ignore[misc]


@pytest.mark.parametrize(
    ('config', 'expected'),
    [
        ({'flowId': 'saved'}, 'stored:saved'),
        ({'flowBuiltin': 'endpoint'}, 'builtin:endpoint'),
        ({'flowDefinition': {}}, 'inline'),
    ],
)
def test_unavailable_selection_reference_is_canonical(config, expected):
    assert unavailable_selected_flow_reference(config) == expected


def test_unavailable_selection_and_runtime_crash_share_terminal_port():
    settled = []
    ports = OrchestrationChatFailurePorts(
        finalize_error=lambda task, error, **kwargs: settled.append(
            (task, error, kwargs)),
    )
    missing = {
        'id': 'missing-flow-task',
        'config': {'flowId': 'deleted', 'model': 'model-a'},
    }
    unavailable = finalize_unavailable_orchestration_chat_flow(
        missing, ports=ports)
    crashed = {'id': 'crashed-flow-task', 'config': {'model': 'model-b'}}
    fatal = finalize_orchestration_chat_flow_exception(
        crashed, RuntimeError('executor exploded'),
        label='flow(stored:saved)', ports=ports,
    )

    assert unavailable['kind'] == 'bad_request'
    assert unavailable['retryable'] is False
    assert missing['_flow_label'] == 'flow(stored:deleted)'
    assert fatal['kind'] == 'internal'
    assert fatal['context'] == 'orchestration-flow-fatal'
    assert fatal['model'] == 'model-b'
    assert [entry[2]['endpoint_reason'] for entry in settled] == [
        'definition_unavailable', 'fatal',
    ]


def test_failure_core_has_one_lazy_task_manager_binding():
    source = (ROOT / 'lib/orchestration_chat_failure.py').read_text()
    defaults = source[source.index('    def defaults('):source.index(
        '\n\ndef unavailable_selected_flow_reference')]
    core = source[source.index(
        'def unavailable_selected_flow_reference'):]

    assert 'from lib.tasks_pkg.manager import finalize_chat_task_error' \
        in defaults
    assert 'from lib.tasks_pkg' not in core
    assert core.count('ports.finalize_error(') == 2
