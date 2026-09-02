#!/usr/bin/env python3
"""Route-boundary guards for project-brain failure attribution.

Pre-existing drift found during api-contract batch 2 (2026-08-01): the
``except`` block of ``project_brain_influence`` had NO return — its
``return api_internal_error(e, source='api_v1.project.brain_influence')``
sat orphaned after ``project_brain_peer_abort``'s except block as dead
code. Effect: an influence failure fell off the end of the function
(Quart ``None`` return → framework 500), losing the route-level
``source=`` diagnostic field.

The contract is asserted through the registered HTTP handlers: each failing
service must return its own typed 500 source, never fall through or borrow a
sibling route's diagnostic identity.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _error_source(response) -> str:
    payload = response.get_json()
    error = payload.get('error') or {}
    return error.get('source') if isinstance(error, dict) else ''


def test_influence_failure_returns_its_own_typed_500(
        flask_client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError('influence failed')

    monkeypatch.setattr(
        'lib.conversations.project_brain_influence.build_conv_influence', fail)
    response = flask_client.get(
        '/api/v1/project/brain/influence?path=/proj/x&convId=conv-a')
    assert response.status_code == 500
    assert _error_source(response) == 'api_v1.project.brain_influence'


def test_peer_abort_failure_keeps_its_own_typed_500(
        flask_client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError('peer abort failed')

    monkeypatch.setattr('lib.conversations.project_peer.intervene_peer', fail)
    response = flask_client.post(
        '/api/v1/project/brain/peer-abort',
        json={'path': '/proj/x', 'convId': 'conv-a', 'toConvId': 'conv-b'},
    )
    assert response.status_code == 500
    assert _error_source(response) == 'api_v1.project.brain_peer_abort'
