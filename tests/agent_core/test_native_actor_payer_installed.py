"""Real SDK transport preserves actor payer history across three executions.

This standalone SDK fixture explicitly supplies accepted/rebound account facts.
It does not replace the application's still-fenced atomic actor handoff owner.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from lib.agent_core.native_model_control import ModelPricing
from lib.agent_core.native_process import Budget, NativeProcessClient
from lib.billing.cost import native_task_cost, transfer_native_child_fees
from lib.billing.native_actor_fees import capture
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_task_terminal import principal
from tests.billing.test_recovered_actor_fee_transfer import root, child
from tests.tasks_pkg.test_native_actor_fee_checkpoint import policy_for

pytestmark = pytest.mark.api


def test_installed_actor_payer_transport_does_not_replay_or_reprice_paid_generations(tmp_path):
    archive = os.environ.get('TOFU_NATIVE_MODEL_CONTROL_TEST_ARCHIVE')
    if not archive:
        pytest.skip('explicit verified native archive required')
    seen, tools, snapshots, invoices = [], [], [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            ordinal = len(seen); seen.append(body)
            if ordinal < 2:
                delta = {'tool_calls': [{'index': 0, 'id': 'call-'+str(ordinal), 'type': 'function',
                    'function': {'name': 'lookup', 'arguments': '{}'}}]}
                finish = 'tool_calls'
            else:
                delta, finish = {'content': 'verified answer'}, 'stop'
            wire = ('data: '+json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire))); self.end_headers(); self.wfile.write(wire)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}); thread.start()
    config = dict(version='tofu.native-agent-stdio/v1', type='configure', owner_user_id=7,
        task_id='actor-0', epoch='epoch', endpoint=f'http://127.0.0.1:{server.server_port}/chat',
        headers={}, template={'model': 'fixture', 'max_tokens': 16},
        messages=[{'role': 'user', 'content': 'finish three stages'}],
        tools={'lookup': {'name': 'lookup', 'contractVersion': 'tofu.tool-contract/v2', 'permission': 'read',
            'arguments_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}},
        event_bytes=65536, output_bytes=65536, tool_calls=8, request_bytes=65536, rounds=4,
        retained_bytes=65536, feedback_bytes=4096, tool_frame_bytes=65536, deadline_ms=10000,
        round_control=True, checkpoint_control=True, checkpoint_binding='actor-fees',
        checkpoint_frame_bytes=65536, checkpoint_shared_account=True, checkpoint_actor_account=True,
        policy=dict(connections=1, idle_connections=1, keepalive_ms=1000, connect_ms=1000,
            request_headers_ms=2000, pool_wait_ms=1000, stream_idle_ms=1000,
            drain_timeout_ms=100, drain_bytes=65536, drain_chunks=16),
        model_control={'ack_ms': 5000, 'pricing': True},
        model_counter=dict(encoding='cl100k_base', output_field='max_tokens', input_bytes=65536,
            text_bytes=65536, tool_json_bytes=65536, depth=128, worker_stack_bytes=1048576))
    async def run(executor):
        previous_cut, previous_actor, original_rows = None, None, []
        for generation in range(3):
            parent, account = root('root-'+str(generation))
            if previous_cut is not None:
                account.restore_pending_frontier(parent, previous_cut, owner=7)
            cuts = []
            account.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
            actor = child(parent, 'actor-'+str(generation)); actor['abort_event'] = threading.Event()
            if previous_actor is not None:
                account.rebind_actor_prefix(actor, source_task_id=previous_actor, owner=7)
            costs = policy_for(actor)
            model = NativeModelAccountPolicy(principal=principal(), task=actor, executor=executor,
                prepare_pricing=lambda _result: ModelPricing.resolved(dict(prompt_tokens=3,
                    input=2.5+generation, output=10., cache_write_multiplier=1.25,
                    cache_read_multiplier=.1, usd_to_cny=7.24, currency='USD', margin=.125)))
            async def accepted_actor_account(request, state, counter_policy):
                # Trusted SDK composition has already restored and rebound the
                # paid frontier. Application recovery still refuses this path.
                assert request.task_id == actor['id']
                assert account.capture_actor_account(owner=7, task=actor,
                    counter_policy=counter_policy) == state
            model.restore_checkpoint = accepted_actor_account
            class Checkpoints:
                binding = 'actor-fees'
                async def load(self, request):
                    if not snapshots: return None
                    state = deepcopy(snapshots[-1])
                    state['task_id'] = actor['id']
                    state['host_fee_projection']['task_id'] = actor['id']
                    state['shared_model_account']['root_task_id'] = parent['id']
                    state['shared_model_account']['actor_task_id'] = actor['id']
                    return state
                async def begin(self, request):
                    assert request.round == generation
                async def complete(self, request):
                    fees = request.state['host_fee_projection']
                    assert 'child_rounds' not in fees and 'wallet_settlement' not in fees
                    assert [row['billable_task_id'] for row in fees['rounds']] == [
                        'root-'+str(i) for i in range(generation+1)]
                    snapshots.append(deepcopy(request.state))
                    return 'suspend'
            async def lookup(request):
                tools.append((actor['id'], request.name))
                return 'feedback-'+actor['id']
            client = NativeProcessClient.from_archive(archive=Path(archive),
                sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
                application_version='0.18.0', principal=principal(),
                budget=Budget(1, 256*1024*1024, 65536, 65536, 5))
            try:
                result = await client.run(dict(config, task_id=actor['id']), callbacks={'lookup': lookup},
                    working_directory=tmp_path, round_policy=costs, model_policy=model,
                    checkpoint_policy=Checkpoints())
                assert result['status'] == ('aborted' if generation < 2 else 'done')
            finally:
                await client.close()
            assert len(seen) == generation+1 and len(actor['apiRounds']) == generation+1
            for historical, original in zip(actor['apiRounds'], original_rows):
                assert historical['nativePrice'] == original['nativePrice']
                assert historical['nativeCredits'] == original['nativeCredits']
                assert historical['taskId'] == original['taskId']
            original_rows = deepcopy(actor['apiRounds'])
            previous_cut, previous_actor = deepcopy(cuts[-1]), actor['id']
            actor['status'] = 'done'
            transfer_native_child_fees(parent, actor); account.close(actor)
            charged = native_task_cost(parent, model='alpha').micro
            assert charged == actor['apiRounds'][-1]['nativeCredits']['total']
            invoices.append(charged)
            proof = capture(actor)
            assert [row['payer_task_id'] for row in proof['rounds']] == ['root-'+str(i) for i in range(generation+1)]
        assert len(seen) == 3 and tools == [('actor-0', 'lookup'), ('actor-1', 'lookup')] and len(snapshots) == 2
        assert [(row['tool_call_id'], row['content']) for row in seen[-1]['messages']
                if row['role'] == 'tool'] == [('call-0', 'feedback-actor-0'), ('call-1', 'feedback-actor-1')]
        assert len(invoices) == 3 and invoices[0] < invoices[-1]
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(run(executor))
    finally:
        server.shutdown(); thread.join(); server.server_close()
