"""Controlled real-provider HTTP/CLI run: receipts survive empty-stop recovery.

No external model or credentials. Each child uses one loopback provider and an
isolated workspace. The scripted provider requests one read only, then either
responds to the recovery instruction or exhausts the existing two-retry budget.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tests.support.standalone_model_routing import standalone_model_routing_envelope

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('recover', [True, False])
def test_cli_recovers_from_real_empty_sse_without_replaying_completed_tool(tmp_path, recover):
    fixture = tmp_path / 'evidence.txt'
    fixture.write_text('single-read-receipt', encoding='utf-8')
    requests_seen = []

    class Provider(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests_seen.append(request)
            call_number = len(requests_seen)
            if call_number == 1:
                delta = {'tool_calls': [{
                    'index': 0, 'id': 'read-once', 'type': 'function',
                    'function': {'name': 'read_files', 'arguments': json.dumps({'path': str(fixture)})},
                }]}
                finish = 'tool_calls'
            elif recover and any(
                    'TERMINAL RESPONSE RECOVERY' in str(m.get('content'))
                    for m in request.get('messages', [])):
                delta = {'content': 'Inspection complete; single-read-receipt verified.'}
                finish = 'stop'
            else:
                delta, finish = {}, 'stop'
            frames = [
                {'choices': [{'index': 0, 'delta': delta}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                 'usage': {'prompt_tokens': 200, 'completion_tokens': 10}},
            ]
            wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write((wire + 'data: [DONE]\n\n').encode())
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret='test-only',
        model_id='harness-stream-test')
    for model in envelope['model_routing']['models']:
        model['capabilities'].append('tools')
    for offering in envelope['model_routing']['offerings']:
        offering['capabilities'].append('tools')
    env = {**os.environ,
           'PYTHONPATH': str(ROOT),
           'TOFU_AGENT_MODEL_ROUTING': json.dumps(envelope),
           'TOFU_DISABLE_CONFIGURED_SLOTS': '1',
           'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
           'LLM_DEBUG_RAW_SSE': '',
           'TOFU_HOME': str(tmp_path / 'home')}
    result_path = tmp_path / 'run.json'
    try:
        proc = subprocess.run([
            sys.executable, '-m', 'tofu_agent', '--env-file', str(tmp_path / 'absent.env'),
            'run', '--task', 'Read evidence.txt once and report its contents.',
            '--cwd', str(tmp_path), '--output', str(result_path), '--timeout-s', '45',
        ], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
    assert result_path.exists(), proc.stderr[-3000:]
    result = json.loads(result_path.read_text())
    assert result['ok'] is recover, (result.get('error'), proc.stderr[-2000:])
    assert proc.returncode == (0 if recover else 6), proc.stderr[-2000:]
    assert len(requests_seen) == (3 if recover else 4)
    for request in requests_seen[1:]:
        receipts = [m for m in request['messages'] if m.get('role') == 'tool']
        assert len(receipts) == 1
        assert 'single-read-receipt' in str(receipts[0]['content'])
    diagnostics = result['stream_diagnostics']
    assert any(r['state'] == 'empty_response' for r in diagnostics)
    assert all(r['finish_seen'] and r['done_seen'] for r in diagnostics)
    assert all(r['transport_bytes'] > 0 and r['malformed_frames'] == 0 for r in diagnostics)
    steps = result['trajectory']['steps']
    calls = [c for s in steps for c in s.get('tool_calls', [])]
    assert len(calls) == 1
    assert calls[0]['function_name'] == 'read_files'
    observations = [r for s in steps for r in s.get('observation', {}).get('results', [])]
    assert len(observations) == 1
    assert observations[0]['source_call_id'] == calls[0]['tool_call_id']
    if not recover:
        assert result['finish_reason'] == 'abnormal_stop'
        assert 'bucket=empty_stop' in result['error']['raw']
