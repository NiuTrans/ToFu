"""Structured source beats adopt only a completed, authorized Agent response."""
import json
import threading

import pytest

pytestmark = pytest.mark.unit


def test_source_agent_abort_after_stream_never_adopts_json(monkeypatch):
    from lib.motion_video import _recipe
    from lib.llm.llm_errors import AbortedError
    aborted = threading.Event()
    def stream(_messages, **kwargs):
        kwargs['on_content']('{"beats": []}')
        aborted.set()
        return {'content': '{"beats": []}'}, 'stop', {}
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', stream)
    with pytest.raises(AbortedError):
        _recipe.script_stage_for_source('Verified source.', abort_check=aborted.is_set)


def test_source_agent_rejects_ungranted_tool(monkeypatch):
    from lib.motion_video import _recipe
    def stream(_messages, **kwargs):
        assert kwargs['tools'] == []
        return {'tool_calls': [{'id': 'invented', 'type': 'function',
            'function': {'name': 'delete_source', 'arguments': '{}'}}]}, 'tool_calls', {}
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', stream)
    with pytest.raises(ValueError, match='ungranted tool'):
        _recipe.script_stage_for_source('Verified source.')


def test_source_agent_rejects_json_without_provider_finish(monkeypatch):
    from lib.motion_video import _recipe
    from lib.llm.stream_result import UnverifiedProviderStreamError
    def stream(_messages, **kwargs):
        content = json.dumps({'beats': [{'text': 'An orphaned draft.'}]})
        kwargs['on_content'](content)
        return {'content': content}, None, {}
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', stream)
    with pytest.raises(UnverifiedProviderStreamError):
        _recipe.script_stage_for_source('Verified source.')


def test_source_agent_restart_discards_partial_content(monkeypatch):
    from lib.motion_video import _recipe
    def stream(_messages, **kwargs):
        kwargs['on_content']('{"orphaned":')
        kwargs['on_attempt_restart']('retry')
        content = json.dumps({'beats': [{'text': 'Verified claim.', 'on_screen': 'Claim'} for _ in range(3)]})
        kwargs['on_content'](content)
        return {'content': content}, 'stop', {}
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream', stream)
    assert _recipe.script_stage_for_source('Verified source.')[0]['text'] == 'Verified claim.'


def test_paper_source_adapter_does_not_treat_abort_as_extractable_fallback(monkeypatch):
    from lib.paper.video_abstract import _llm_beats
    from lib.llm.llm_errors import AbortedError
    def abort(*_args, **_kwargs):
        raise AbortedError('explicit stop')
    monkeypatch.setattr('lib.motion_video._recipe.script_stage_for_source', abort)
    with pytest.raises(AbortedError):
        _llm_beats('Source.', lang='en', max_scenes=8, capacity=88)
