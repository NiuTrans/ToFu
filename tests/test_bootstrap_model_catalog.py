"""First-run bootstrap must prefer the account's live model catalogue."""

from __future__ import annotations

import json
import socket

import pytest

import bootstrap

pytestmark = pytest.mark.unit


def _model(model_id, capabilities=None, **extra):
    row = {
        'model_id': model_id,
        'aliases': [],
        'capabilities': capabilities or ['text'],
        'rpm': 30,
        'cost': 0.01,
        'thinking_default': False,
    }
    row.update(extra)
    return row


def test_stale_template_selection_falls_back_to_live_cheap_chat_model():
    models = [
        _model('expensive-current'),
        _model('cheap-current', ['text', 'cheap']),
        _model('embedding', ['embedding']),
    ]
    assert bootstrap._bootstrap_choose_model(models, 'retired-template') == \
        'cheap-current'
    assert bootstrap._bootstrap_choose_model(models, 'expensive-current') == \
        'expensive-current'


def test_bootstrap_template_fallback_is_managed_and_keeps_wire_pool():
    models = bootstrap._bootstrap_template_models(
        'https://api.example.test/v1', [{
            'base_url': 'https://api.example.test/v1',
            'models': [{
                'model_id': 'logical', 'request_ids': ['wire-a', 'wire-b'],
                'capabilities': ['text'],
            }],
        }])
    assert models[0]['request_ids'] == ['wire-a', 'wire-b']
    assert models[0]['catalog_managed'] is True
    assert models[0]['catalog_source'] == 'template'


def test_bootstrap_persists_live_provider_and_selected_default(
        tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, '_bootstrap_data_root', lambda: str(tmp_path))
    models = [
        _model('live'),
        _model('private', catalog_pinned=True, catalog_source='manual'),
    ]
    bootstrap._bootstrap_persist_provider(
        'https://api.example.test/v1', 'secret', models,
        templates=[{
            'key': 'example', 'name': 'Example', 'brand': 'generic',
            'base_url': 'https://api.example.test/v1', 'models': models,
        }],
        default_model='live',
    )
    cfg = json.loads(
        (tmp_path / 'config/server_config.json').read_text(encoding='utf-8'))
    provider = cfg['providers'][0]
    assert provider['api_keys'] == ['secret']
    assert provider['models'] == models
    assert provider['model_catalog_sync'] == {'mode': 'auto'}
    assert cfg['presets']['opus'] == 'live'
    assert cfg['models']['LLM_MODEL'] == 'live'
    assert cfg['model_defaults']['default_model'] == 'live'

    # A later bootstrap refresh replaces managed rows but never loses a
    # private deployment the user explicitly pinned.
    bootstrap._bootstrap_persist_provider(
        'https://api.example.test/v1', 'secret', [_model('new-live')],
        templates=[{
            'key': 'example', 'name': 'Example', 'brand': 'generic',
            'base_url': 'https://api.example.test/v1', 'models': [],
        }],
        default_model='new-live',
    )
    cfg = json.loads(
        (tmp_path / 'config/server_config.json').read_text(encoding='utf-8'))
    assert [m['model_id'] for m in cfg['providers'][0]['models']] == [
        'new-live', 'private']


def test_bootstrap_discovery_rejects_link_local_target(monkeypatch):
    monkeypatch.setattr(
        bootstrap.socket, 'getaddrinfo',
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 80))
        ])
    monkeypatch.setattr(
        bootstrap.urllib.request, 'build_opener',
        lambda *_args: pytest.fail('blocked endpoint must not be requested'))
    assert bootstrap._bootstrap_discover_models(
        'http://metadata.invalid/v1', 'secret') == []


def test_bootstrap_discovery_parses_authenticated_live_catalogue(monkeypatch):
    monkeypatch.setattr(
        bootstrap.socket, 'getaddrinfo',
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
        ])

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({'data': [
                {'id': 'new-mini'}, {'id': 'ft:private'}, {'id': 'new-mini'},
            ]}).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://api.example.test/v1/models'
            assert request.get_header('Authorization') == 'Bearer secret'
            assert timeout == 3
            return Response()

    monkeypatch.setattr(
        bootstrap.urllib.request, 'build_opener', lambda *_args: Opener())
    models = bootstrap._bootstrap_discover_models(
        'https://api.example.test/v1', 'secret', templates=[], timeout=3)
    assert [m['model_id'] for m in models] == ['new-mini']
    assert set(models[0]['capabilities']) == {'text', 'cheap'}
    assert models[0]['catalog_source'] == 'provider'
