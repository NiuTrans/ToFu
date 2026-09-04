"""Owner-scoped v2 contracts for the local endpoint health monitor."""

from __future__ import annotations

import pytest

from lib.model_routing import (
    InMemoryModelRoutingRepository,
    OwnerBoundary,
    empty_document,
    upsert_local_provider,
)


pytestmark = pytest.mark.unit

ENDPOINT = 'http://10.0.0.5:8000/v1'


def _model(model_id: str) -> dict:
    return {
        'model_id': model_id,
        'capabilities': ['text'],
        'rpm': 30,
    }


def _routing(provider_id: str = 'auto_vllm_8000'):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(7, 'tenant-a')
    repository.compare_and_swap(
        boundary, empty_document(), expected_revision=0)
    upsert_local_provider(
        repository,
        boundary,
        provider_id=provider_id,
        display_name='Local test',
        base_url=ENDPOINT,
        models=[_model('qwen')],
    )
    return repository, boundary


def _pending_ids(repository, boundary) -> set[str]:
    return {
        row['pending_model_id']
        for row in repository.get(boundary).document['offerings']
    }


def test_model_drift_resyncs_v2_without_periodic_revision_churn(monkeypatch):
    import lib.llm_dispatch.health_local as health_local

    repository, boundary = _routing()
    rebuilds = []
    monkeypatch.setattr(health_local, '_get_dispatcher', lambda: None)
    monkeypatch.setattr(
        health_local,
        '_check_endpoint',
        lambda _url, _key: {
            'ok': True,
            'status': 'ok',
            'served_models': {'qwen', 'llama'},
            'effective_url': ENDPOINT,
        },
    )
    monkeypatch.setattr(
        health_local,
        'discover_models',
        lambda _url, _key: [_model('qwen'), _model('llama')],
    )
    monkeypatch.setattr(
        health_local, '_rebuild_dispatcher_slots',
        lambda: rebuilds.append(True))
    monkeypatch.setattr(health_local, 'RESYNC_EVERY', 1)
    health_local._success_streak.clear()

    first = health_local.check_once(
        boundary=boundary, repository=repository)
    first_revision = repository.get(boundary).revision
    second = health_local.check_once(
        boundary=boundary, repository=repository)

    assert first['resynced'] == 1
    assert _pending_ids(repository, boundary) == {'qwen', 'llama'}
    assert second['resynced'] == 0
    assert repository.get(boundary).revision == first_revision
    assert rebuilds == [True]


def test_down_endpoint_keeps_authority_and_attempts_slot_cooldown(monkeypatch):
    import lib.llm_dispatch.health_local as health_local

    repository, boundary = _routing()
    before = repository.get(boundary)
    cooled = []
    monkeypatch.setattr(
        health_local,
        '_check_endpoint',
        lambda _url, _key: {'ok': False, 'status': 'timeout'},
    )
    monkeypatch.setattr(
        health_local,
        '_cooldown_endpoint_slots',
        lambda provider_id, endpoint, seconds: (
            cooled.append((provider_id, endpoint, seconds)) or 1),
    )
    monkeypatch.setattr(health_local, '_check_ephemeral_endpoints', lambda: {
        'endpoints_ok': 0, 'cooldowns': 0})

    stats = health_local.check_once(
        boundary=boundary, repository=repository)

    assert stats['cooldowns'] == 1
    assert repository.get(boundary).revision == before.revision
    assert _pending_ids(repository, boundary) == {'qwen'}
    assert cooled == [('auto_vllm_8000', ENDPOINT, health_local.COOLDOWN_ON_DEAD)]


def test_user_authored_local_provider_is_observed_but_never_rewritten(monkeypatch):
    import lib.llm_dispatch.health_local as health_local

    repository, boundary = _routing(provider_id='user-local')
    before = repository.get(boundary)
    monkeypatch.setattr(health_local, '_get_dispatcher', lambda: None)
    monkeypatch.setattr(
        health_local,
        '_check_endpoint',
        lambda _url, _key: {
            'ok': True,
            'status': 'ok',
            'served_models': {'different'},
            'effective_url': ENDPOINT,
        },
    )
    monkeypatch.setattr(
        health_local,
        'discover_models',
        lambda *_args: pytest.fail('user-authored provider must not be rewritten'),
    )

    stats = health_local.check_once(
        boundary=boundary, repository=repository)

    assert stats['endpoints_ok'] == 1
    assert stats['resynced'] == 0
    assert repository.get(boundary).revision == before.revision


class _Response:
    def __init__(self, ok, status, payload=None):
        self.ok = ok
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_check_endpoint_falls_back_to_v1_and_reports_effective(monkeypatch):
    import lib.llm_dispatch.health_local as health_local

    def fake_get(url, **_kwargs):
        if url.endswith('/v1/models'):
            return _Response(True, 200, {'data': [{'id': 'qwen3'}]})
        return _Response(False, 404)

    monkeypatch.setattr(health_local, 'http_get', fake_get)
    result = health_local._check_endpoint('http://10.0.0.5:11434', '')

    assert result['ok'] is True
    assert result['served_models'] == {'qwen3'}
    assert result['effective_url'] == 'http://10.0.0.5:11434/v1'


def test_check_endpoint_no_fallback_on_timeout(monkeypatch):
    import requests
    import lib.llm_dispatch.health_local as health_local

    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        raise requests.Timeout('boom')

    monkeypatch.setattr(health_local, 'http_get', fake_get)
    result = health_local._check_endpoint('http://10.0.0.5:11434', '')

    assert result['ok'] is False and result['status'] == 'timeout'
    assert calls == ['http://10.0.0.5:11434/models']
