"""tests/test_inprocess_facade.py — in-process façade + shared kernel.

Covers the pure, no-network surface of:
  * lib.tasks_pkg.entry.build_chat_config — body→cfg field mapping + precedence
  * lib.tasks_pkg.entry.ChatResult        — terminal projection / .ok logic
  * lib.tasks_pkg.entry._assemble_result  — task dict → ChatResult
  * tofu package                          — public surface + re-exports

The orchestrator-driving paths (run_chat_sync / run_chat_stream) need a live
LLM + task runtime, so they're exercised by integration tests, not here.

Run:  pytest tests/test_inprocess_facade.py -m unit -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestBuildChatConfig:
    """build_chat_config mirrors the HTTP route's body→cfg mapping."""

    def test_model_and_basic_knobs(self):
        from lib.tasks_pkg.entry import build_chat_config
        cfg = build_chat_config('gpt-x', None, max_tokens=1024, temperature=0.5)
        assert cfg['model'] == 'gpt-x'
        assert cfg['maxTokens'] == 1024
        assert cfg['temperature'] == 0.5

    def test_response_format_maps_to_camelcase(self):
        from lib.tasks_pkg.entry import build_chat_config
        rf = {'type': 'json_object'}
        cfg = build_chat_config('m', None, response_format=rf)
        assert cfg['responseFormat'] == rf

    def test_tools_mapped(self):
        from lib.tasks_pkg.entry import build_chat_config
        tools = [{'type': 'function', 'function': {'name': 'foo'}}]
        cfg = build_chat_config('m', None, tools=tools)
        assert cfg['tools'] == tools

    def test_explicit_config_wins_over_knobs(self):
        """An explicit config value must take precedence over the top-level
        knob — same precedence the HTTP route documents."""
        from lib.tasks_pkg.entry import build_chat_config
        cfg = build_chat_config(
            'm', {'maxTokens': 99, 'responseFormat': {'type': 'text'}},
            max_tokens=1024, response_format={'type': 'json_object'})
        assert cfg['maxTokens'] == 99
        assert cfg['responseFormat'] == {'type': 'text'}

    def test_none_knobs_do_not_inject_keys(self):
        from lib.tasks_pkg.entry import build_chat_config
        cfg = build_chat_config('m', None)
        assert 'maxTokens' not in cfg
        assert 'temperature' not in cfg
        assert 'tools' not in cfg
        assert 'responseFormat' not in cfg
        # thinkingDepth is always seeded (route parity), defaulting to ''
        assert cfg['thinkingDepth'] == ''

    def test_thinking_depth_seeded(self):
        from lib.tasks_pkg.entry import build_chat_config
        cfg = build_chat_config('m', None, thinking_depth='high')
        assert cfg['thinkingDepth'] == 'high'

    def test_user_only_when_present(self):
        from lib.tasks_pkg.entry import build_chat_config
        assert 'user' not in build_chat_config('m', None)
        assert build_chat_config('m', None, user='alice')['user'] == 'alice'

    def test_does_not_mutate_caller_config(self):
        from lib.tasks_pkg.entry import build_chat_config
        original = {'maxTokens': 10}
        cfg = build_chat_config('m', original, temperature=0.1)
        assert cfg is not original
        assert 'temperature' not in original


@pytest.mark.unit
class TestRouteKernelParity:
    """The HTTP route must produce the SAME cfg the kernel produces, so the
    two chat surfaces can't drift on how knobs land in cfg."""

    def test_route_uses_build_chat_config(self):
        import inspect
        from routes.api_v1 import chat as chat_route
        src = inspect.getsource(chat_route.chat_completions)
        assert 'build_chat_config' in src, (
            'chat_completions must route field-mapping through the shared '
            'build_chat_config kernel')

    def test_kernel_matches_legacy_mapping(self):
        """Replicate the pre-refactor inline mapping and assert the kernel
        yields an identical cfg for a representative body."""
        from lib.tasks_pkg.entry import build_chat_config

        body = {
            'max_tokens': 2048, 'temperature': 0.3,
            'tools': [{'type': 'function', 'function': {'name': 'x'}}],
            'response_format': {'type': 'json_object'},
            'thinking_depth': 'max', 'user': 'bob',
        }
        # Legacy inline behaviour (from routes/api_v1/chat.py before refactor)
        legacy = {'model': 'm'}
        legacy.setdefault('thinkingDepth', body.get('thinking_depth') or
                          body.get('thinkingDepth') or '')
        if 'max_tokens' in body and 'maxTokens' not in legacy:
            legacy['maxTokens'] = body.get('max_tokens')
        if 'temperature' in body and 'temperature' not in legacy:
            legacy['temperature'] = body.get('temperature')
        if 'tools' in body and 'tools' not in legacy:
            legacy['tools'] = body.get('tools')
        if 'response_format' in body and 'responseFormat' not in legacy:
            legacy['responseFormat'] = body.get('response_format')
        if body.get('user'):
            legacy.setdefault('user', body['user'])

        kernel = build_chat_config(
            'm', {},
            max_tokens=body.get('max_tokens'),
            temperature=body.get('temperature'),
            tools=body.get('tools'),
            response_format=body.get('response_format'),
            thinking_depth=body.get('thinking_depth') or '',
            user=body.get('user') or '',
        )
        assert kernel == legacy


@pytest.mark.unit
class TestChatResult:
    """ChatResult projection + .ok semantics."""

    def test_assemble_from_done_task(self):
        from lib.tasks_pkg.entry import _assemble_result
        task = {
            'id': 'abc', 'status': 'done', 'content': 'Hello',
            'thinking': 'reasoning', 'finishReason': 'stop',
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
            'toolRounds': [{'tool_calls': [{'id': 'tc1'}]}],
            'error': None,
        }
        res = _assemble_result(task)
        assert res.content == 'Hello'
        assert res.thinking == 'reasoning'
        assert res.tool_calls == [{'id': 'tc1'}]
        assert res.usage['prompt_tokens'] == 10
        assert res.finish_reason == 'stop'
        assert res.task_id == 'abc'
        assert res.ok is True
        assert res.raw_task is task

    def test_ok_false_on_error_envelope(self):
        from lib.tasks_pkg.entry import _assemble_result
        task = {
            'id': 'x', 'status': 'done', 'content': '',
            'error': {'kind': 'ratelimit', 'message': 'slow down'},
        }
        res = _assemble_result(task)
        assert res.ok is False
        assert res.error['kind'] == 'ratelimit'

    def test_ok_false_on_non_done_status(self):
        from lib.tasks_pkg.entry import ChatResult
        assert ChatResult(status='error').ok is False
        assert ChatResult(status='aborted').ok is False
        assert ChatResult(status='done').ok is True


@pytest.mark.unit
class TestTofuFacadeSurface:
    """The public tofu package exposes the documented surface."""

    def test_exports(self):
        import tofu
        assert hasattr(tofu, 'chat')
        assert hasattr(tofu, 'stream')
        assert hasattr(tofu, 'capabilities')
        assert tofu.__api_version__ == 'v1'

    def test_chatresult_reexported(self):
        import tofu
        from lib.tasks_pkg.entry import ChatResult as KernelResult
        assert tofu.ChatResult is KernelResult

    def test_billing_and_byo_not_in_surface(self):
        """Billing + BYO are HTTP-only by design — they must NOT leak into
        the in-process façade."""
        import tofu
        names = set(dir(tofu))
        for forbidden in ('reserve', 'settle', 'debit', 'provider',
                          'ephemeral', 'mint_ephemeral_slot'):
            assert forbidden not in names
