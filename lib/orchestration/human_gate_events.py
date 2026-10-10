"""Canonical event constructors for orchestration human-gate lifecycle."""

from __future__ import annotations

from typing import Literal

from lib.orchestration.events import event_preview


HumanGateResolution = Literal[
    'approved', 'rejected', 'answered', 'cancelled',
]


def human_gate_notify_event(
    *, node_id: object, name: object, prompt: object,
) -> dict:
    return {
        'type': 'human_notify',
        'node_id': node_id,
        'name': str(name or 'Human'),
        'prompt': str(prompt or ''),
    }


def human_gate_request_event(
    *, node_id: object, name: object, mode: object,
    prompt: object, request_id: object,
) -> dict:
    return {
        'type': 'human_request',
        'node_id': node_id,
        'name': str(name or 'Human'),
        'mode': str(mode or ''),
        'prompt': str(prompt or ''),
        'request_id': str(request_id or ''),
    }


def human_gate_resolved_event(
    *, node_id: object, mode: object, request_id: object,
    resolution: HumanGateResolution, approved: bool | None = None,
    answer: object | None = None,
) -> dict:
    event = {
        'type': 'human_resolved',
        'node_id': node_id,
        'mode': str(mode or ''),
        'request_id': str(request_id or ''),
        'resolution': resolution,
    }
    if approved is not None:
        event['approved'] = bool(approved)
    if answer is not None:
        event['preview'] = event_preview(answer)
    return event


__all__ = [
    'HumanGateResolution',
    'human_gate_notify_event',
    'human_gate_request_event',
    'human_gate_resolved_event',
]
