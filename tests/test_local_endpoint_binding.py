#!/usr/bin/env python3
"""Local discovery preserves the effective `/v1` endpoint.

Model-routing v2 represents endpoint/model placement explicitly as one
Deployment per Connection, so the retired `endpoint_models` fan-out tests do
not belong at runtime. These tests retain the discovery boundary that feeds
that contract: bare origins retry `/v1/models`, and the effective URL reaches
probe results without hiding meaningful failures.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

class _Resp:
    def __init__(self, ok, status, payload=None):
        self.ok = ok
        self.status_code = status
        self._payload = payload or {}
        self.text = '' if ok else 'not found'

    def json(self):
        return self._payload


def _fake_http_get():
    calls = []

    def fake(url, headers=None, timeout=None, **kw):
        calls.append(url)
        if url.endswith('/v1/models'):
            return _Resp(True, 200, {'data': [{'id': 'qwen3'}]})
        return _Resp(False, 404)

    return fake, calls


@pytest.mark.unit
def test_discover_retries_bare_origin_under_v1(monkeypatch):
    import lib.llm_dispatch.discovery as disco_pkg
    fake, calls = _fake_http_get()
    monkeypatch.setattr(disco_pkg, 'http_get', fake, raising=False)
    from lib.llm_dispatch.discovery import discover_models
    models = discover_models('http://10.0.0.5:11434', '')
    assert [m['model_id'] for m in models] == ['qwen3'], \
        'bare-origin /models 404 must be retried under /v1 (ollama habit)'
    assert calls[0] == 'http://10.0.0.5:11434/models', \
        'the direct URL must be tried FIRST'
    assert 'http://10.0.0.5:11434/v1/models' in calls, \
        'fallback must append /v1 exactly once'


@pytest.mark.unit
def test_discover_reports_effective_base_url(monkeypatch):
    import lib.llm_dispatch.discovery as disco_pkg
    fake, _ = _fake_http_get()
    monkeypatch.setattr(disco_pkg, 'http_get', fake, raising=False)
    from lib.llm_dispatch.discovery import discover_models
    models, effective = discover_models('http://10.0.0.5:11434', '',
                                        return_effective=True)
    assert [m['model_id'] for m in models] == ['qwen3']
    assert effective == 'http://10.0.0.5:11434/v1', \
        'caller must learn the WORKING base URL so the stored endpoint is usable'


@pytest.mark.unit
def test_periodic_discovery_can_silence_only_clean_not_found(monkeypatch,
                                                            caplog):
    """An unrelated listener on a well-known port is expected probe noise.

    The opt-in must suppress both fallback 404s without weakening visibility
    for a genuine upstream/server failure.
    """
    import logging
    import lib.llm_dispatch.discovery as disco_pkg
    from lib.llm_dispatch.discovery import discover_models

    monkeypatch.setattr(
        disco_pkg, 'http_get',
        lambda *_a, **_k: _Resp(False, 404), raising=False)
    with caplog.at_level(logging.DEBUG):
        assert discover_models(
            'http://10.0.0.5:11434', '', quiet_not_found=True) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert sum('returned HTTP 404' in r.getMessage()
               for r in caplog.records) == 2
    assert not [
        r for r in caplog.records
        if r.levelno >= logging.INFO
        and ('Fetching models' in r.getMessage()
             or 'Received ' in r.getMessage()
             or 'usable models' in r.getMessage())
    ], 'best-effort background discovery must keep routine fetch logs at DEBUG'

    caplog.clear()
    monkeypatch.setattr(
        disco_pkg, 'http_get',
        lambda *_a, **_k: _Resp(False, 503), raising=False)
    with caplog.at_level(logging.DEBUG):
        assert discover_models(
            'http://10.0.0.5:11434', '', quiet_not_found=True) == []
    assert any(r.levelno >= logging.WARNING and
               'returned HTTP 503' in r.getMessage()
               for r in caplog.records)


@pytest.mark.unit
def test_probe_result_carries_effective_v1_base_url(monkeypatch):
    import lib.llm_dispatch.discovery as disco_pkg
    fake, _ = _fake_http_get()
    monkeypatch.setattr(disco_pkg, 'http_get', fake, raising=False)
    from lib.llm_dispatch.discovery import probe_provider
    res = probe_provider('http://10.0.0.5:11434', '', force_local=True)
    assert res.get('ok') is True, 'probe of a bare ollama origin must succeed via /v1'
    assert res.get('base_url') == 'http://10.0.0.5:11434/v1', \
        'probe must return the effective /v1 base URL (chat calls would 404 on the bare origin)'
    assert [m['model_id'] for m in res.get('models', [])] == ['qwen3']


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
