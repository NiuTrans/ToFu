"""SDK release metadata and both client transports identify the same version."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tomllib

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_python_sdk_release_identity(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'clients/python'))
    from tofu_sdk import Tofu, __version__
    from tofu_sdk.async_client import AsyncTofu

    metadata = tomllib.loads((ROOT / 'clients/python/pyproject.toml').read_text())
    expected = metadata['project']['version']
    assert __version__ == expected
    client = Tofu(base_url='https://sdk.invalid')
    try:
        assert client._session.headers['User-Agent'] == f'tofu-sdk-python/{expected}'
    finally:
        client._session.close()

    async def check_async():
        client = AsyncTofu(base_url='https://sdk.invalid')
        try:
            assert client._client.headers['User-Agent'] == f'tofu-sdk-python/{expected}'
        finally:
            await client._client.aclose()

    asyncio.run(check_async())
