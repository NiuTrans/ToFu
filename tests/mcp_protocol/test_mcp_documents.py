"""MCP application documents retain exact typed payloads and cancellable waits."""
import asyncio
import threading
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from lib.mcp.client import MCPBridge, _MCPServerHandle
from lib.mcp.documents import MCPDocumentError, decode_document, encode_document
from lib.mcp.types import MCP_MAX_DOCUMENT_BYTES, MCP_MAX_DOCUMENT_NODES, MCP_MAX_RESULT_CHARS

pytestmark = pytest.mark.unit


@pytest.fixture
def connection():
    bridge = MCPBridge()
    handle = _MCPServerHandle('papers', {'timeout': 30})
    name = 'mcp__papers__bundle'
    bridge._servers['papers'] = handle
    bridge._tool_index[name] = {
        'server_name': 'papers', 'tool_name': 'bundle', 'namespaced_name': name,
        'input_schema': {}, 'openai_def': {}, 'read_only_hint': True,
    }
    yield bridge, handle, name
    # Exercise the owning bridge's disposal path; no transport was connected.
    bridge._servers.clear()
    bridge.disconnect_all()


class Session:
    def __init__(self, result):
        self.result = result

    async def call_tool(self, *args, **kwargs):
        return self.result


def result(payload=None, text='Build complete', **extra):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=text)],
                           structured_content=payload, **extra)


def test_application_document_survives_human_text_and_display_truncation(connection):
    bridge, handle, name = connection
    payload = {'chunk': 'x' * (MCP_MAX_RESULT_CHARS + 1000)}
    handle.session = Session(result(payload))
    value = bridge.call_tool(name, {})
    assert 'Build complete' in value and '[Truncated:' in value
    assert isinstance(value, str) and decode_document(value) == payload
    assert bridge.call_tool_document(name, {}, timeout=30) == payload


@pytest.mark.parametrize('payload', [{}, [], {'ok': True}])
def test_structured_empty_documents_remain_distinct_from_absent_payload(connection, payload):
    bridge, handle, name = connection
    handle.session = Session(result(payload))
    assert bridge.call_tool_document(name, {}, timeout=30) == payload


@pytest.mark.parametrize('text', ['Error: compile failed', 'MCP Error: expired',
    'Built successfully\n{"build_id":"partial"}', '{"build_id":"partial"'])
def test_legacy_errors_or_mixed_display_are_never_build_documents(connection, text):
    bridge, handle, name = connection
    handle.session = Session(result(text=text))
    with pytest.raises(MCPDocumentError):
        bridge.call_tool_document(name, {}, timeout=30)


def test_exact_legacy_json_is_supported_and_protocol_error_flags_win(connection):
    bridge, handle, name = connection
    handle.session = Session(result(text='{"ok":true}'))
    assert bridge.call_tool_document(name, {}, timeout=30) == {'ok': True}
    for spelling in ('is_error', 'isError'):
        handle.session = Session(result({'ok': True}, **{spelling: True}))
        with pytest.raises(MCPDocumentError, match='returned an error'):
            bridge.call_tool_document(name, {}, timeout=30)


def test_legacy_json_uses_original_bytes_before_display_truncation(connection):
    import json
    bridge, handle, name = connection
    payload = {'chunk': 'x' * (MCP_MAX_RESULT_CHARS + 1000)}
    handle.session = Session(result(text=json.dumps(payload)))
    value = bridge.call_tool(name, {})
    assert '[Truncated:' in value
    assert decode_document(value) == payload


@pytest.mark.parametrize('payload', [
    {'blob': 'x' * MCP_MAX_DOCUMENT_BYTES},
    {'blob': '\u4e2d' * (MCP_MAX_DOCUMENT_BYTES // 2)},
    [0] * MCP_MAX_DOCUMENT_NODES,
    {'invalid': float('nan')}, {'invalid': '\ud800'},
])
def test_oversized_or_invalid_structured_content_cannot_fall_back_to_success_text(connection, payload):
    bridge, handle, name = connection
    handle.session = Session(result(payload, text='{"ok":true}'))
    value = bridge.call_tool(name, {})
    if isinstance(payload, dict) and isinstance(payload.get('blob'), str):
        assert value.structured_json is not None  # canonical retention, not receipt acceptance
    else:
        assert 'Structured result omitted' in value
        assert value.structured_json is None
    with pytest.raises(MCPDocumentError):
        decode_document(value)


def test_nested_and_cyclic_documents_are_bounded():
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(MCPDocumentError, match='structure limit'):
        encode_document(cyclic)
    with pytest.raises(MCPDocumentError):
        decode_document('[' * 80 + '0' + ']' * 80)


def test_cancellation_retires_the_active_sdk_call_without_retry(connection):
    bridge, handle, name = connection
    entered, retired, cancel = threading.Event(), threading.Event(), threading.Event()
    class WaitingSession:
        async def call_tool(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                retired.set()
    handle.session = WaitingSession()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(bridge.call_tool_document, name, {}, timeout=30, cancel=cancel)
        assert entered.wait(2)
        cancel.set()
        with pytest.raises(CancelledError):
            future.result(timeout=2)
        assert retired.wait(2)
    assert not bridge._active_calls and not bridge._timeout_streak


def test_pre_cancelled_document_never_enters_sdk(connection):
    bridge, handle, name = connection
    class ForbiddenSession:
        async def call_tool(self, *args, **kwargs):
            pytest.fail('A cancelled caller must not dispatch an MCP request')
    handle.session = ForbiddenSession()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        bridge.call_tool_document(name, {}, timeout=30, cancel=cancel)


def test_ambiguous_transport_loss_does_not_replay_an_application_operation(connection, monkeypatch):
    from anyio import ClosedResourceError
    bridge, handle, name = connection
    calls = []
    class LostReplySession:
        async def call_tool(self, *args, **kwargs):
            calls.append(args)
            raise ClosedResourceError()
    handle.session = LostReplySession()
    def no_retry(*args):
        pytest.fail('A lost build/write response must not replay the operation')
    monkeypatch.setattr(bridge, '_reconnect_server', no_retry)
    with pytest.raises(ClosedResourceError):
        bridge.call_tool_document(name, {}, timeout=30)
    assert len(calls) == 1


def test_outer_deadline_cancels_the_underlying_coroutine(connection):
    bridge, _, _ = connection
    retired = threading.Event()
    async def wait_forever():
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()
    with pytest.raises(TimeoutError):
        bridge._run_async(wait_forever(), timeout=.05)
    assert retired.wait(2)


@pytest.mark.parametrize('timeout', [True, 0, -1])
def test_application_documents_validate_explicit_deadlines(connection, timeout):
    bridge, _, name = connection
    with pytest.raises(ValueError, match='positive integer'):
        bridge.call_tool_document(name, {}, timeout=timeout)


@pytest.mark.parametrize('timeout',[None,420])
def test_application_document_accepts_default_and_long_explicit_deadline(connection,timeout):
    bridge,handle,name=connection
    payload={'ok':True};handle.session=Session(result(payload))
    assert bridge.call_tool_document(name,{},timeout=timeout)==payload
