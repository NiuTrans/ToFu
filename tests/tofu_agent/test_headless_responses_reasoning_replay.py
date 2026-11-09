"""Headless `tofu-agent run` on a Responses route that streams unrequested ciphertext.

The 2026-11-09 TB2.1 pilot ran gpt-6.1-sol through your-llm-gateway.example.com's Kunlun face
(``kunlunbedrock.gpt-6.1-sol-m17``), which returns a ``reasoning`` item carrying
only ``encrypted_content`` whether or not the request asked for it. Its envelope
did not declare ``responses_reasoning_replay``, and the run died after the first
reasoning round (``protocol_conversion:mandatory_continuity_unrepresentable``,
exit 4, reward 0). A scripted loopback endpoint plays that face; the real CLI,
routing admission, dispatch, tools and transport run from this checkout, and
Tofu's catalog supplies the gpt-6.1-sol reasoning profile. No network, keys or
model spend.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tests.support.loopback import LoopbackThreadingHTTPServer

pytestmark = pytest.mark.api
ROOT = Path(__file__).resolve().parents[2]
TOOL_ROUNDS = 5
FINAL_ANSWER = f'DONE after {TOOL_ROUNDS} tool rounds'
MODEL = {'creator_id': 'openai', 'model_id': 'gpt-6.1-sol'}


def _envelope(base_url: str, *, declared: bool) -> dict:
    """The TB harness envelope on the Kunlun Responses face."""
    features = {'cache_affinity_mt_session_id': 'supported'}
    if declared:
        features['responses_reasoning_replay'] = 'supported'
    capabilities = ['text', 'thinking', 'tools']
    return {
        'model_routing': {
            'contract_version': 'tofu.model-routing/v2', 'revision': 0,
            'creators': [{'creator_id': 'openai', 'name': 'openai'}],
            'models': [{**MODEL, 'display_name': 'gpt-6.1-sol', 'capabilities': capabilities,
                        'context_window': 262144, 'quality_rank': 10}],
            'providers': [{'provider_id': 'example-corp', 'name': 'example-corp', 'scope': 'owner'}],
            'provider_accesses': [{'provider_access_id': 'example-corp-access',
                                   'provider_id': 'example-corp', 'enabled': True,
                                   'quota_policy': {}}],
            'connections': [{'connection_id': 'example-corp-kunlun',
                             'provider_access_id': 'example-corp-access', 'base_url': base_url,
                             'protocol': 'openai_responses', 'responses_profile': 'compatible',
                             'enabled': True, 'priority': 0, 'extra_headers': {},
                             'inference_profile': {'features': features}}],
            'credentials': [{'credential_id': 'example-corp-credential',
                             'provider_access_id': 'example-corp-access', 'kind': 'api_key',
                             'secret_reference': 'example-corp-secret', 'key_hint': 'configured',
                             'enabled': True,
                             'authorization': {'connection_ids': ['example-corp-kunlun'],
                                               'models': [dict(MODEL)]},
                             'quota_policy': {}}],
            'offerings': [{'offering_id': 'example-corp-offering',
                           'provider_access_id': 'example-corp-access',
                           'identity_state': 'confirmed', 'model': dict(MODEL), 'enabled': True,
                           'capabilities': capabilities, 'context_window': 262144,
                           'priority': 0}],
            'deployments': [{'deployment_id': 'example-corp-kunlun-gpt-6.1-sol',
                             'offering_id': 'example-corp-offering',
                             'connection_id': 'example-corp-kunlun',
                             'wire_model_id': 'kunlunbedrock.gpt-6.1-sol-m17', 'enabled': True,
                             'identity_confidence': 'high', 'probe_status': 'passed',
                             'priority': 0, 'max_output_tokens': 8192}],
        },
        'model': dict(MODEL),
        'routing': {'preferred_provider_id': 'example-corp'},
        'credential_secrets': {'example-corp-secret': 'synthetic-not-a-key'},
    }


def _kunlun_like_endpoint(requests_seen: list[dict]) -> LoopbackThreadingHTTPServer:
    class Endpoint(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            items = body.get('input') or []
            round_num = 1 + sum(item.get('type') == 'function_call_output' for item in items)
            requests_seen.append({
                'round': round_num, 'include': body.get('include'),
                'context': (body.get('reasoning') or {}).get('context'),
                'replayed': [item.get('id') for item in items
                             if item.get('type') == 'reasoning']})
            reasoning = {'type': 'reasoning', 'id': f'rs_{round_num}', 'summary': [],
                         'encrypted_content': f'kunlun-like-ciphertext-{round_num}'}
            if round_num <= TOOL_ROUNDS:
                second = {'type': 'function_call', 'id': f'fc_{round_num}',
                          'call_id': f'call_{round_num}', 'name': 'run_command',
                          'arguments': json.dumps({'command': f'echo round-{round_num}'}),
                          'status': 'completed'}
            else:
                second = {'type': 'message', 'id': 'msg_final', 'role': 'assistant',
                          'status': 'completed', 'content': [{
                              'type': 'output_text', 'annotations': [],
                              'text': FINAL_ANSWER}]}
            output = [reasoning, second]
            events = [{'type': 'response.created', 'response': {
                'id': f'resp_{round_num}', 'status': 'in_progress', 'output': []}}]
            for index, item in enumerate(output):
                events.append({'type': 'response.output_item.added',
                               'output_index': index, 'item': item})
                events.append({'type': 'response.output_item.done',
                               'output_index': index, 'item': item})
            events.append({'type': 'response.completed', 'response': {
                'id': f'resp_{round_num}', 'status': 'completed', 'output': output,
                'usage': {'input_tokens': 1000, 'output_tokens': 60,
                          'output_tokens_details': {'reasoning_tokens': 28}}}})
            payload = b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n'
                               for event in events)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize('declared', [False, True],
                         ids=['replay-undeclared', 'replay-declared'])
def test_headless_run_completes_every_tool_round_on_a_kunlun_like_route(tmp_path, declared):
    requests_seen: list[dict] = []
    server = _kunlun_like_endpoint(requests_seen)
    workspace = tmp_path / 'work'
    for directory in (workspace, tmp_path / 'home', tmp_path / 'data'):
        directory.mkdir()
    result_path = tmp_path / 'result.json'
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('TOFU_')
                   and key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    environment.update(
        PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1',
        HOME=str(tmp_path / 'home'), TOFU_DATA_DIR=str(tmp_path / 'data'),
        TOFU_AGENT_MODEL_ROUTING=json.dumps(_envelope(
            f'http://127.0.0.1:{server.server_port}/v1', declared=declared)),
        NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
    try:
        completed = subprocess.run([
            sys.executable, '-m', 'tofu_agent', '--env-file', str(tmp_path / 'absent.env'),
            'run', '--task', 'Run the commands the task needs, then report DONE.',
            '--cwd', str(workspace), '--output', str(result_path), '--timeout-s', '120',
            '--tools', '*', '--thinking', 'medium', '--trajectory', '',
        ], cwd=workspace, env=environment, capture_output=True, text=True, timeout=240)
    finally:
        server.shutdown()
        server.server_close()

    result = json.loads(result_path.read_text(encoding='utf-8'))
    assert completed.returncode == 0, (result.get('error'), completed.stderr[-2000:])
    assert (result['status'], result['n_tool_rounds'], result['content']) == (
        'done', TOOL_ROUNDS, FINAL_ANSWER)
    assert [row['round'] for row in requests_seen] == list(range(1, TOOL_ROUNDS + 2))
    for row in requests_seen:
        if declared:
            assert row['include'] == ['reasoning.encrypted_content']
            assert row['context'] == 'all_turns'
            assert row['replayed'] == [f'rs_{prior}' for prior in range(1, row['round'])]
        else:
            assert (row['include'], row['context'], row['replayed']) == (None, None, [])
