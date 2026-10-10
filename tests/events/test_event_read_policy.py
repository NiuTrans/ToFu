"""tests/events/test_event_read_policy.py — Required-on-write / required-on-read policy.

Pins the two halves of the event-vocabulary integrity rule
(docs/contracts/EVENTS.md §Versioning):

* **Required-on-write**: ``build_event`` — the one typed construction
  chokepoint for the governed vocabulary — rejects UNREGISTERED types
  (strict under pytest; warn-and-log in production). Forward compatibility
  is achieved by REGISTERING the new type (additive, no version bump), never
  by emitting an undeclared one.
* **Required-on-read**: durable reconstruction paths refuse rows whose
  ``type`` is not in the registry — an unknown type means the log was
  written by a newer runtime or corrupted, and silently folding past it
  would resume a gutted state. ``fold_cold_state`` converts the refusal
  into its existing authoritative-checkpoint fallback with a loud warning.
"""

from __future__ import annotations

import logging
import uuid

import pytest

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]


def test_build_event_rejects_unregistered_type():
    from lib.agent_core.events import EventContractError, build_event
    with pytest.raises(EventContractError, match='unregistered event type'):
        build_event('definitely_not_registered', x=1)


def test_build_event_accepts_registered_type():
    from lib.agent_core.events import build_event
    assert build_event('delta', content='x') == {'type': 'delta', 'content': 'x'}


def test_assert_registered_event_types_accepts_both_row_shapes():
    """read_events rows (``{'event_id', 'payload'}``) and raw event dicts are
    both accepted, mirroring the duality fold_text_from_events documents."""
    from lib.agent_core.events import assert_registered_event_types
    rows = [
        {'event_id': 0, 'payload': {'type': 'delta', 'content': 'a'}},
        {'type': 'phase', 'phase': 'working'},
    ]
    assert_registered_event_types(rows, consumer='test')  # no raise


def test_assert_registered_event_types_raises_naming_consumer_and_types():
    from lib.agent_core.events import (
        EventContractError,
        assert_registered_event_types,
    )
    rows = [
        {'payload': {'type': 'delta'}},
        {'payload': {'type': 'future_event_a'}},
        {'type': 'future_event_b'},
    ]
    with pytest.raises(EventContractError) as excinfo:
        assert_registered_event_types(rows, consumer='fold-probe')
    message = str(excinfo.value)
    assert 'fold-probe' in message
    assert 'future_event_a' in message and 'future_event_b' in message


def test_fold_cold_state_unknown_type_refuses_fold_and_warns(caplog):
    """A durable row of unknown vocabulary must not be folded into a silently
    truncated state: the fold refuses, the warning names the type, and the
    authoritative checkpoint wins — even when it is SHORTER than the folded
    text would have been (over-refusing beats silent truncation)."""
    from lib.tasks_pkg.event_fold import fold_cold_state_text
    from lib.tasks_pkg.event_log import append_persistent_event, flush_pending
    task_id = f'fold-unknown-{uuid.uuid4().hex[:8]}'
    append_persistent_event(task_id, 0, {'type': 'delta', 'content': 'kept text'})
    append_persistent_event(task_id, 1, {'type': 'from_a_newer_runtime'})
    flush_pending(task_id)
    with caplog.at_level(logging.WARNING):
        content, _thinking = fold_cold_state_text(
            task_id, checkpoint_content='ck')
    assert content == 'ck', (
        f'unknown durable type must refuse the fold, got {content!r}')
    assert any('from_a_newer_runtime' in record.message
               for record in caplog.records), (
        'the refusal must name the unknown type in the warning')


def test_informational_registry_marker_matches_spec():
    """The five informational events are the transient presentation/transport
    class; everything else defaults to durable."""
    from lib.agent_core.events import get_event_spec
    for type_ in ('phase', 'tool_progress', 'presence', 'sse_timeout', 'ping'):
        assert get_event_spec(type_).read_policy == 'informational', type_
    assert get_event_spec('delta').read_policy == 'durable'
