"""Owner and request-authority propagation across multi-dispatch fallbacks."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_smart_chat_direct_fallback_preserves_owner_tools_and_extra(
        monkeypatch):
    from lib.llm_dispatch import api

    captured = {}
    monkeypatch.setattr(
        'lib.llm_dispatch._api_multi.dispatch_chat',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError('dispatch unavailable')),
    )

    def _chat(**kwargs):
        captured.update(kwargs)
        return 'ok', {}

    monkeypatch.setattr('lib.llm.chat', _chat)
    tools = [{'type': 'function', 'function': {
        'name': 'lookup', 'parameters': {'type': 'object'}}}]

    content, _usage = api.smart_chat(
        [{'role': 'user', 'content': 'use the tool'}],
        tools=tools,
        extra={'response_format': {'type': 'json_object'}},
        owner_user_id=41,
    )

    assert content == 'ok'
    assert captured['owner_user_id'] == 41
    assert captured['extra']['tools'] == tools
    assert captured['extra']['response_format'] == {'type': 'json_object'}


def test_parallel_task_owner_override_does_not_treat_zero_as_missing(
        monkeypatch):
    import lib.llm_dispatch._api_multi as multi

    captured = []

    def _dispatch(_messages, **kwargs):
        captured.append(kwargs['owner_user_id'])
        return 'ok', {}

    monkeypatch.setattr(multi, 'dispatch_chat', _dispatch)
    results = multi.dispatch_parallel(
        [
            {'messages': [{'role': 'user', 'content': 'outer'}]},
            {'messages': [{'role': 'user', 'content': 'invalid'}],
             'owner_user_id': 0},
        ],
        owner_user_id=41,
        max_workers=1,
    )

    assert results == [('ok', {}), ('ok', {})]
    assert captured == [41, 0]
