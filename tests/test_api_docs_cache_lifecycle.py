"""OpenAPI projections are isolated by live app identity without leaks."""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest
from quart import Quart

import routes.api_docs as api_docs


pytestmark = pytest.mark.unit


def test_openapi_cache_reuses_live_app_and_releases_disposed_app(monkeypatch) -> None:
    calls: list[str] = []

    def build(app: Quart) -> dict:
        calls.append(app.name)
        return {"openapi": "3.1.0", "info": {"title": app.name}, "paths": {}}

    monkeypatch.setattr(api_docs, "build_spec", build)
    with api_docs._cached_specs_lock:
        api_docs._cached_specs.clear()

    app = Quart("ephemeral-openapi-app", static_folder=None)
    app_reference = weakref.ref(app)
    async def read_twice(target: Quart) -> tuple[dict, dict]:
        async with target.app_context():
            return api_docs._spec(), api_docs._spec()

    first, second = asyncio.run(read_twice(app))
    assert first is second
    assert calls == ["ephemeral-openapi-app"]
    assert len(api_docs._cached_specs) == 1

    del app, first, second
    gc.collect()

    assert app_reference() is None
    assert len(api_docs._cached_specs) == 0
