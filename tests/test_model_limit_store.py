"""Bounded persistence contracts for auto-learned output-token limits."""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.unit


def _redirect_config(monkeypatch, tmp_path):
    import lib.config_dir as config_dir

    path = tmp_path / 'server_config.json'
    real = config_dir.config_path
    monkeypatch.setattr(
        config_dir,
        'config_path',
        lambda *parts: str(path) if parts == ('server_config.json',)
        else real(*parts),
    )
    return path


def test_load_repairs_invalid_values_and_model_ids(monkeypatch, tmp_path):
    from lib.model_info import _limits as limits_store

    path = _redirect_config(monkeypatch, tmp_path)
    path.write_text(json.dumps({
        'unrelated': {'kept': True},
        'model_limits': {
            ' valid-with-space ': 1024,
            'm' * 257: 2048,
            'good': '4096',
            'too-large': 1_000_001,
        },
    }), encoding='utf-8')

    assert limits_store._load_learned_limits() == {'good': 4096}
    repaired = json.loads(path.read_text(encoding='utf-8'))
    assert repaired['unrelated'] == {'kept': True}
    assert repaired['model_limits'] == {'good': 4096}


def test_capacity_retains_newest_insertion_order():
    from lib.model_info._limits import _sanitize_learned_limits

    limits, changed = _sanitize_learned_limits(
        {'old': 100, 'middle': 200, 'new': 300},
        max_entries=2,
    )

    assert changed is True
    assert limits == {'middle': 200, 'new': 300}


def test_learn_rejects_unbounded_identity_and_value(monkeypatch, tmp_path):
    from lib.model_info import _limits as limits_store

    path = _redirect_config(monkeypatch, tmp_path)
    monkeypatch.setattr(limits_store, '_LEARNED_MODEL_LIMITS', {})

    limits_store._learn_model_limit('m' * 257, 1024)
    limits_store._learn_model_limit('valid', 1_000_001)
    limits_store._learn_model_limit('valid', True)

    assert limits_store._LEARNED_MODEL_LIMITS == {}
    assert not path.exists()


def test_learn_preserves_other_config_and_moves_refresh_to_tail(
    monkeypatch,
    tmp_path,
):
    from lib.model_info import _limits as limits_store

    path = _redirect_config(monkeypatch, tmp_path)
    path.write_text(json.dumps({
        'unrelated': 7,
        'model_limits': {'refresh': 100, 'other': 200},
    }), encoding='utf-8')
    monkeypatch.setattr(
        limits_store,
        '_LEARNED_MODEL_LIMITS',
        {'refresh': 100, 'other': 200},
    )

    limits_store._learn_model_limit('refresh', 300)

    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert persisted['unrelated'] == 7
    assert list(persisted['model_limits']) == ['other', 'refresh']
    assert persisted['model_limits']['refresh'] == 300


def test_route_limit_persists_without_poisoning_global_model_limit(
    monkeypatch,
    tmp_path,
):
    from lib.model_info import _limits as limits_store

    path = _redirect_config(monkeypatch, tmp_path)
    path.write_text(json.dumps({
        'unrelated': 7,
        'model_limits': {'gemini': 100_000},
    }), encoding='utf-8')
    monkeypatch.setattr(limits_store, '_LEARNED_ROUTE_LIMITS', {})
    route_key = limits_store._route_output_limit_key(
        provider_id='provider', offering_id='offering',
        deployment_id='deployment', protocol='openai', model='gemini')

    limits_store._learn_model_limit(
        'gemini', 65_535, route_key=route_key)

    persisted = json.loads(path.read_text(encoding='utf-8'))
    assert persisted['unrelated'] == 7
    assert persisted['model_limits'] == {'gemini': 100_000}
    assert persisted['route_model_limits'] == {route_key: 65_535}
