"""System-prompt preview faults obey the shared HTTP 500 boundary."""

from __future__ import annotations

import asyncio

import pytest
from tests.support.envelope import v1_get_json


pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.api]


@pytest.mark.parametrize('path', [
    '/api/v4/system-prompt/default',
    '/api/v4/system-prompt/blocks',
])
def test_prompt_preview_failure_is_redacted(flask_app, monkeypatch, path):
    from routes.api_v1 import capabilities

    monkeypatch.setattr(
        capabilities._PROMPT_CACHE,
        'get_or_compute',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('/secret/prompt/path?token=do-not-expose')),
    )

    async def _run():
        response = await flask_app.test_client().get(path)
        return response.status_code, await v1_get_json(response)

    status, body = asyncio.run(_run())
    assert status == 500
    assert body['ok'] is False
    assert '/secret/prompt/path' not in str(body)
    assert 'do-not-expose' not in str(body)
