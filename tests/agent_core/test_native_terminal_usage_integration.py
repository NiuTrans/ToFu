"""Explicit real native process, SDK, terminal projection and retry; no paid calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

import pytest

from lib.agent_core.native_process import Budget, NativeProcessClient
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy, project_native_terminal


@pytest.mark.unit
@pytest.mark.parametrize("reference",[False,True])
@pytest.mark.parametrize("retry_reference",[False,True])
@pytest.mark.parametrize("history_bytes",[5,1<<20])
def test_actual_native_summary_replaces_host_accumulation_and_survives_commit_retry(tmp_path,reference,retry_reference,history_bytes):
    configured = os.environ.get('TOFU_NATIVE_USAGE_TEST_BINARY')
    if not configured:
        pytest.skip('explicit locally built native binary required')
    binary = Path(configured).resolve(strict=True)
    calls = []
    history="hello" if history_bytes==5 else "x"*history_bytes
    frame_bytes=6*history_bytes+65536

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            frame = {'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 3, 'completion_tokens': 2,
                               'prompt_tokens_details': {'cached_tokens': 1}}}
            wire = ('data: ' + json.dumps(frame) + '\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

        def log_message(self, *_args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()

    async def run():
        owner = PrincipalContext.user(subject_id='native-usage-test', owner_user_id=7)
        client = NativeProcessClient(binary=binary, sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            protocol_version='tofu.native-agent-stdio/v1', principal=owner,
            budget=Budget(1, binary.stat().st_size, frame_bytes, frame_bytes, 3))
        task = {'id': 'task', '_userId': 7, 'status': 'running'}
        commits = []
        def commit(current):
            commits.append(dict(current['usage']))
            return len(commits) > 1
        config = dict(version=client.protocol_version, type='configure', owner_user_id=7,
            task_id='task', epoch='epoch', endpoint=f'http://127.0.0.1:{server.server_port}/chat',
            headers={}, template={'model': 'fixture'}, messages=[{'role': 'user', 'content': history}],
            tools={}, event_bytes=frame_bytes, output_bytes=frame_bytes, tool_calls=8, request_bytes=frame_bytes,
            rounds=3, retained_bytes=frame_bytes, feedback_bytes=4096, tool_frame_bytes=8192, deadline_ms=5000,
            policy=dict(connections=1, idle_connections=1, keepalive_ms=1000, connect_ms=1000,
                request_headers_ms=2000, pool_wait_ms=1000, stream_idle_ms=1000,
                drain_timeout_ms=100, drain_bytes=frame_bytes, drain_chunks=16),
            terminal_persistence=dict(operation_id='commit', ack_ms=2000, attempts=2,
                frame_bytes=frame_bytes, usage_summary=True))
        if reference:config["terminal_persistence"]["completion_reference"]=True
        if retry_reference:config["terminal_persistence"]["retry_reference"]=True
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                terminal = NativeTaskTerminalPolicy(principal=owner, task=task, operation_id='commit',
                    executor=executor, project=project_native_terminal, candidate_bytes=frame_bytes)
                with patch('lib.tasks_pkg.native_terminal._persist', side_effect=commit), patch(
                        'lib.cost.accumulate_usage_into', side_effect=AssertionError('Python recounted native usage')):
                    event = await client.run(config, callbacks={}, working_directory=tmp_path,
                                             terminal_policy=terminal)
                assert terminal.accepted and event['status'] == 'done'
            assert commits == [task['usage'], task['usage']]
            assert task['usage'] == {'prompt_tokens': 3, 'completion_tokens': 2,
                                     'prompt_tokens_details': {'cached_tokens': 1}}
            assert event['result']['usage'][0]['prompt_tokens'] == 3
            assert event['result']['messages'][0]=={'role':'user','content':history}
            assert task['messages']==event['result']['messages']
            assert len(calls) == 1 and client.active_runs == 0
        finally:
            await client.close()

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
