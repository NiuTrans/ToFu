"""Behavioral contract for the memory-route project-path resolver.

The JSON branch is intentionally parsed once because every request_json call
crosses the Quart sync bridge. A non-empty JSON value takes precedence;
otherwise the shared query decoder owns proxy-path normalization.
"""

from __future__ import annotations

import asyncio

from quart import Quart
import pytest


pytestmark = pytest.mark.unit


def _run_async(coroutine):
    """Drive Quart's async request context without pytest-asyncio."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


def test_project_path_parses_json_once_and_takes_precedence(monkeypatch):
    import lib.request_parser as request_parser
    import routes.api_v1.memory as memory_routes

    body_calls: list[bool] = []

    def request_json_once(*, silent=False):
        body_calls.append(bool(silent))
        return {'project_path': '/from-body'}

    def query_decoder_must_not_run(_name):
        raise AssertionError('query fallback ran despite an explicit JSON path')

    monkeypatch.setattr(memory_routes, 'request_json', request_json_once)
    monkeypatch.setattr(
        request_parser, 'decode_proxy_path_arg', query_decoder_must_not_run)

    async def exercise_request():
        app = Quart(__name__)
        async with app.test_request_context(
                '/api/v1/memory?project_path=%2Ffrom-query',
                method='POST',
                json={'project_path': '/from-body'}):
            assert memory_routes._project_path() == '/from-body'

    _run_async(exercise_request())

    assert body_calls == [True], 'JSON body must cross the sync bridge once'


def test_project_path_uses_shared_query_decoder_without_json(monkeypatch):
    import lib.request_parser as request_parser
    import routes.api_v1.memory as memory_routes

    query_calls: list[str] = []

    def query_decoder(name):
        query_calls.append(name)
        return '/from-query'

    def body_parser_must_not_run(*_args, **_kwargs):
        raise AssertionError('non-JSON request unexpectedly parsed a body')

    monkeypatch.setattr(memory_routes, 'request_json', body_parser_must_not_run)
    monkeypatch.setattr(request_parser, 'decode_proxy_path_arg', query_decoder)

    async def exercise_request():
        app = Quart(__name__)
        async with app.test_request_context(
                '/api/v1/memory?project_path=%2Ffrom-query', method='GET'):
            assert memory_routes._project_path() == '/from-query'

    _run_async(exercise_request())

    assert query_calls == ['project_path']
