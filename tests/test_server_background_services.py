from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from lib.server_background_services import (
    start_background_services,
    start_lan_discovery_responder,
    stop_lan_discovery_responder,
)


pytestmark = pytest.mark.unit


def test_persisted_configuration_precedes_network_owned_services(monkeypatch):
    import lib.desktop.pairing as pairing
    import lib.integration_control as integration_control
    import lib.motion_video.engine as motion_engine
    import lib.netpath as netpath
    import lib.paper.podcast_engine.worker as podcast_engine
    import lib.research.api as research_api
    import lib.slides.api as slides_api
    import routes

    calls: list[str] = []
    monkeypatch.setattr(
        routes,
        'start_registered_background_services',
        lambda _app, **_kwargs: calls.append('routes'),
    )
    monkeypatch.setattr(
        netpath, 'start_prober', lambda: calls.append('netpath'))
    monkeypatch.setattr(
        integration_control, 'ensure_worker_started', lambda: False)
    monkeypatch.setattr(
        motion_engine, 'resume_interrupted_jobs', lambda: 0)
    monkeypatch.setattr(
        research_api, 'resume_interrupted_research', lambda: 0)
    monkeypatch.setattr(
        slides_api, 'resume_interrupted_decks', lambda: 0)
    monkeypatch.setattr(
        podcast_engine, 'mark_interrupted_podcasts', lambda: None)
    monkeypatch.setattr(
        pairing, 'maybe_start_responder', lambda *_args, **_kwargs: None)

    # Keep this order test focused on the authoritative configuration seams;
    # optional resume integrations remain best-effort and idempotent.
    start_background_services(
        SimpleNamespace(extensions={}),
        load_saved_proxy_config=lambda: calls.append('proxy'),
        bootstrap_personal_key=lambda: calls.append('key'),
        logger=logging.getLogger('test.background-services'),
        environ={
            '_TOFU_RUNTIME_PORT': '15000',
            '_TOFU_RUNTIME_HOST': '127.0.0.1',
            'PYTEST_CURRENT_TEST': '1',
        },
    )

    assert calls[:4] == ['routes', 'proxy', 'key', 'netpath']



def test_distributed_preview_starts_no_background_owner(monkeypatch):
    import routes

    calls = []
    monkeypatch.setattr(
        routes,
        'start_registered_background_services',
        lambda *_args, **_kwargs: calls.append('routes'),
    )
    start_background_services(
        SimpleNamespace(extensions={}),
        process_role='worker',
        load_saved_proxy_config=lambda: calls.append('proxy'),
        bootstrap_personal_key=lambda: calls.append('key'),
        logger=logging.getLogger('test.distributed-preview'),
        environ={
            'TOFU_DEPLOYMENT_MODE': 'distributed',
            'TOFU_DISTRIBUTED_PREVIEW_MODE': 'read-only',
        },
    )
    assert calls == []


def test_lan_discovery_has_one_exact_lifecycle_owner(monkeypatch):
    import lib.desktop.pairing as pairing
    import lib.server_background_services as background_services

    class Responder:
        def __init__(self):
            self.running = True
            self.stops = []

        def is_running(self):
            return self.running

        def stop(self, timeout):
            self.stops.append(timeout)
            self.running = False
            return True

    responder = Responder()
    starts = []
    monkeypatch.setattr(
        background_services, '_LAN_DISCOVERY_RESPONDER', None)
    monkeypatch.setattr(
        pairing, 'maybe_start_responder',
        lambda *args, **kwargs: starts.append((args, kwargs)) or responder)
    env = {'TOFU_DESKTOP_LAN_DISCOVERY': '1'}

    assert start_lan_discovery_responder(
        15000, bind_host='0.0.0.0', environ=env) is responder
    assert start_lan_discovery_responder(
        15000, bind_host='0.0.0.0', environ=env) is responder
    assert len(starts) == 1
    assert starts[0][1]['environ'] is env
    assert stop_lan_discovery_responder(timeout=0.125) is True
    assert responder.stops == [0.125]
    assert background_services._LAN_DISCOVERY_RESPONDER is None
