"""Synchronous credential authorities never run on Quart's event loop thread."""

from __future__ import annotations

import asyncio
import threading

import pytest


pytestmark = pytest.mark.unit


def test_token_validation_runs_on_bounded_default_executor(monkeypatch):
    import routes.api_v1.auth as auth

    caller = threading.current_thread()
    observed = []

    def validate(token):
        observed.append((token, threading.current_thread()))
        return 'context'

    monkeypatch.setattr(auth, 'validate_token', validate)
    result = asyncio.run(auth._validate_token('token'))

    assert result == 'context'
    assert observed[0][0] == 'token'
    assert observed[0][1] is not caller


def test_bridge_credential_resolution_runs_off_event_loop(monkeypatch):
    import lib.bridge_auth as bridge_auth
    import routes.api_v1.auth as auth

    caller = threading.current_thread()
    observed = []

    def resolve(provided, *, allow_process_agent=False):
        observed.append(
            (provided, allow_process_agent, threading.current_thread()))
        return 'bridge-context'

    monkeypatch.setattr(bridge_auth, 'resolve_bridge_credential', resolve)

    async def invoke():
        from quart import Quart

        app = Quart(__name__)
        async with app.test_request_context(
            '/api/desktop/poll',
            headers={'X-Bridge-Secret': 'secret'},
        ):
            return await auth._bridge_auth_context()

    assert asyncio.run(invoke()) == 'bridge-context'
    assert observed[0][:2] == ('secret', True)
    assert observed[0][2] is not caller
