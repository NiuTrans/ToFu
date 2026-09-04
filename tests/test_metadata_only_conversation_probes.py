"""Metadata/existence probes must never hydrate conversation transcripts."""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


pytestmark = pytest.mark.unit


def _conversation_get_payloads(function) -> list[ast.Dict]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    payloads = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        operation = node.args[0]
        payload = node.args[1]
        if (
            isinstance(operation, ast.Constant)
            and operation.value == 'conversation.get'
            and isinstance(payload, ast.Dict)
        ):
            payloads.append(payload)
    return payloads


def _assert_metadata_only(function, *, expected_calls: int = 1) -> None:
    payloads = _conversation_get_payloads(function)
    assert len(payloads) == expected_calls, (
        f'{function.__module__}.{function.__qualname__} has '
        f'{len(payloads)} literal conversation.get calls; expected '
        f'{expected_calls}'
    )
    for payload in payloads:
        fields = {
            key.value: value
            for key, value in zip(payload.keys, payload.values)
            if isinstance(key, ast.Constant)
        }
        selector = fields.get('derive_messages')
        assert isinstance(selector, ast.Constant)
        assert selector.value is False


def test_background_metadata_and_existence_probes_are_transcript_free():
    from lib.scheduler import conversation_dispatch
    from lib.tasks_pkg import autopilot_baton, persistence_store
    from lib.tasks_pkg.manager import _persist
    import lib.turn_lifecycle as turn_lifecycle

    boundaries = (
        (conversation_dispatch.dispatch_scheduled_turn, 1),
        (autopilot_baton._start_followup_task, 1),
        (_persist._upsert_task_row, 1),
        (
            persistence_store.DefaultConversationStore
            .notify_conversation_changed,
            1,
        ),
        (turn_lifecycle.list_turns, 2),
        (turn_lifecycle.get_conversation_revision, 1),
    )
    for function, expected_calls in boundaries:
        _assert_metadata_only(function, expected_calls=expected_calls)
