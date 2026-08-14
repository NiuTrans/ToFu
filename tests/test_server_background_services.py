from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from lib.server_background_services import start_background_services


pytestmark = pytest.mark.unit


def test_persisted_configuration_precedes_network_owned_services(monkeypatch):
    import lib.billing.janitor as janitor
    import lib.desktop.pairing as pairing
    import lib.integration_control as integration_control
    import lib.motion_video.engine as motion_engine
    import lib.netpath as netpath
    import lib.paper.podcast_engine as podcast_engine
    import lib.research as research
    import lib.slides.engine as slides_engine
    import routes

    calls: list[str] = []
    monkeypatch.setattr(
        routes,
        'start_registered_background_services',
        lambda _app: calls.append('routes'),
    )
    monkeypatch.setattr(
        janitor, 'start_janitor', lambda: calls.append('janitor'))
    monkeypatch.setattr(
        netpath, 'start_prober', lambda: calls.append('netpath'))
    monkeypatch.setattr(
        integration_control, 'ensure_worker_started', lambda: False)
    monkeypatch.setattr(
        motion_engine, 'resume_interrupted_jobs', lambda: 0)
    monkeypatch.setattr(
        research, 'resume_interrupted_research', lambda: 0)
    monkeypatch.setattr(
        slides_engine, 'resume_interrupted_decks', lambda: 0)
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

    assert calls[:5] == ['routes', 'proxy', 'key', 'janitor', 'netpath']
