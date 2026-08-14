"""Wire-level guards for MCP 2026-07-28 negotiation and legacy fallback.

The fixture is intentionally a tiny raw stdio peer.  A framework server that
accepts both eras could let a client which still always sends ``initialize``
pass; these peers reject the wrong opening method, so the tests prove which
protocol ChatUI actually speaks on the wire.
"""

from __future__ import annotations

import importlib.metadata
import json
import sys
import textwrap

import pytest


pytestmark = pytest.mark.unit


_RAW_SERVER = textwrap.dedent(
    r'''
    import json
    import sys

    era = sys.argv[1]
    trace_path = sys.argv[2]

    def send(request_id, *, result=None, error=None):
        message = {'jsonrpc': '2.0', 'id': request_id}
        if error is not None:
            message['error'] = error
        else:
            message['result'] = result
        sys.stdout.write(json.dumps(message, separators=(',', ':')) + '\n')
        sys.stdout.flush()

    with open(trace_path, 'a', encoding='utf-8', buffering=1) as trace:
        for line in sys.stdin:
            request = json.loads(line)
            method = request.get('method', '')
            trace.write(method + '\n')
            request_id = request.get('id')

            # Notifications carry no id and receive no response.
            if request_id is None:
                continue

            if method == 'server/discover':
                if era == 'modern':
                    send(request_id, result={
                        'supportedVersions': ['2026-07-28'],
                        'capabilities': {'tools': {}},
                    })
                else:
                    send(request_id, error={
                        'code': -32601,
                        'message': 'Method not found',
                    })
            elif method == 'initialize':
                if era == 'modern':
                    send(request_id, error={
                        'code': -32022,
                        'message': 'initialize is not valid in MCP 2026-07-28',
                        'data': {'supported': ['2026-07-28']},
                    })
                else:
                    send(request_id, result={
                        'protocolVersion': '2025-11-25',
                        'capabilities': {'tools': {}},
                        'serverInfo': {
                            'name': 'raw-legacy',
                            'version': '1.0',
                        },
                    })
            elif method == 'tools/list':
                result = {'tools': [{
                    'name': 'echo_era',
                    'description': 'Return the negotiated fixture era.',
                    'inputSchema': {'type': 'object', 'properties': {}},
                }]}
                if era == 'modern':
                    result.update({
                        'ttlMs': 0,
                        'cacheScope': 'private',
                        'resultType': 'complete',
                    })
                send(request_id, result=result)
            elif method == 'tools/call':
                result = {
                    'content': [{'type': 'text', 'text': era + '-ok'}],
                    'isError': False,
                }
                if era == 'modern':
                    result['resultType'] = 'complete'
                send(request_id, result=result)
            else:
                send(request_id, error={
                    'code': -32601,
                    'message': 'Method not found: ' + method,
                })
    '''
)


def _require_sdk_v2() -> None:
    import mcp

    if not hasattr(mcp, 'Client'):
        pytest.skip(
            'MCP SDK 2 is not installed in this interpreter '
            f'(found {importlib.metadata.version("mcp")}); reinstall ChatUI dependencies'
        )


def _exercise_peer(tmp_path, era: str, *, require_v2: bool = True):
    if require_v2:
        _require_sdk_v2()

    from lib.mcp.client import MCPBridge

    server = tmp_path / f'raw_{era}_mcp.py'
    server.write_text(_RAW_SERVER, encoding='utf-8')
    trace = tmp_path / f'{era}.trace'
    bridge = MCPBridge()
    try:
        tools = bridge.connect_server(
            era,
            {
                'transport': 'stdio',
                'command': sys.executable,
                'args': [str(server), era, str(trace)],
                'enabled': True,
            },
        )
        assert [tool.name for tool in tools] == ['echo_era']
        result = bridge.call_tool(f'mcp__{era}__echo_era', {})
        server_row = bridge.list_servers()[0]
        methods = trace.read_text(encoding='utf-8').splitlines()
        return result, server_row, methods
    finally:
        bridge.disconnect_all()


def test_modern_only_peer_negotiates_2026_without_initialize(tmp_path):
    result, server, methods = _exercise_peer(tmp_path, 'modern')

    assert result == 'modern-ok'
    assert server['protocol_version'] == '2026-07-28'
    assert server['sdk_generation'] == 2
    assert server['compatibility_notice'] is None
    assert methods[0] == 'server/discover'
    assert 'initialize' not in methods


def test_legacy_peer_falls_back_after_discover_rejection(tmp_path):
    result, server, methods = _exercise_peer(
        tmp_path, 'legacy', require_v2=False)

    assert result == 'legacy-ok'
    assert server['protocol_version'] == '2025-11-25'
    assert server['server_impl_name'] == 'raw-legacy'
    assert server['compatibility_notice'] == {
        'kind': 'legacy_protocol',
        'protocol_version': '2025-11-25',
        'target_protocol': '2026-07-28',
        'update_recommended': True,
        'blocking': False,
    }
    import mcp
    if hasattr(mcp, 'Client'):
        assert methods[:2] == ['server/discover', 'initialize']
    else:
        # A rolling deploy may briefly run the new bridge on the old SDK. It
        # cannot probe the new protocol, but must keep legacy peers usable.
        assert methods[0] == 'initialize'
    assert 'notifications/initialized' in methods
