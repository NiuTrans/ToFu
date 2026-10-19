"""Real durable root invocation over a caller-owned store and loopback provider.

RootWorkload borrows the existing storage authority, native host and callback
executor. Both arms execute the mature root preparation/terminal owners; only
the model-loop implementation differs. No live model, store fallback or task
implementation is emulated. Timers include joined native child CPU.
"""
import asyncio
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import resource
import threading
import time
import uuid


def cpu_ns():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return int((own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime) * 1_000_000_000)


def comparison_receipt(rows):
    """Expose exact comparability before any performance verdict is considered.

    Token usage is canonical provider usage; dispatch IDs and elapsed clocks
    are observation metadata and remain outside the equality claim. Decoded
    request values/types are compared; JSON formatting/key order are excluded.
    No raw payload enters the receipt.
    """
    if len(rows) != 2 or [row['arm'] for row in rows] != ['python', 'native']:
        raise ValueError('comparison requires one ordered pair of root arms')
    observations = []
    for row in rows:
        encoded = json.dumps(row['requests'], sort_keys=True, ensure_ascii=True,
                             separators=(',', ':'), allow_nan=False).encode()
        requests = row['requests']
        observations.append(dict(arm=row['arm'], status=row['status'],
            request_sha256=hashlib.sha256(encoded).hexdigest(),
            provider_requests=len(requests),
            wire_tool_names=[[tool['function']['name'] for tool in body.get('tools', [])]
                             for body in requests],
            message_counts=[len(body['messages']) for body in requests],
            usage={key:row['usage'].get(key) for key in
                   ('prompt_tokens', 'completion_tokens', 'total_tokens')}))
    equivalent = (observations[0]['request_sha256'] == observations[1]['request_sha256']
        and rows[0]['status'] == rows[1]['status'] == 'done'
        and rows[0]['content'] == rows[1]['content']
        and observations[0]['usage'] == observations[1]['usage']
        and all(type(value) is int and value >= 0
                for row in observations for value in row['usage'].values()))
    return dict(scope='durable_text_root_comparability', observations=observations,
        equivalent_work=equivalent, measurement_permitted=equivalent,
        performance_admitted=False, default_selection_admitted=False,
        request_comparison='decoded JSON values and types; formatting/key order excluded',
        clock_scope='one diagnostic first operation per arm; not a timing cohort')


class RootWorkload:
    def __init__(self, *, storage, host, executor, directory, budget, resource_policy, scenario='text', cpu_clock=cpu_ns, expected_wire_models=('alpha-wire-a',)):
        self.storage, self.host, self.executor = storage, host, executor
        self.directory, self.budget, self.resource_policy = Path(directory), budget, resource_policy
        self.bodies = []
        self.response_text=None
        self.expected_wire_models=frozenset(expected_wire_models)
        if not self.expected_wire_models or any(not isinstance(value,str) or not value for value in self.expected_wire_models):
            raise ValueError("fixture requires explicit expected wire models")
        self.cpu_clock = cpu_clock
        if scenario not in {'text','discovery_execute','mixed_direct_first','mixed_gateway_first'}:
            raise ValueError('unknown root scenario')
        self.scenario, self.active_start = scenario, 0
        workload = self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert self.path == '/v1/chat/completions'
                assert self.headers['Authorization'] == 'Bearer private-fixture-token'
                assert body['model'] in workload.expected_wire_models
                workload.bodies.append(body)
                ordinal = len(workload.bodies) - workload.active_start - 1
                answer=workload.response_text(ordinal) if workload.response_text is not None else 'durable fixture answer'
                delta, finish = {'content':answer}, 'stop'
                if workload.scenario=='discovery_execute' and ordinal<2:
                    name = 'search_tools' if ordinal==0 else 'execute_tools'
                    arguments = {'query':'todo_write'} if ordinal==0 else {
                        'calls':[{'name':'todo_write','call_id':'nested-todo','arguments':{'todos':[{'content':'Verify native gateway','status':'completed'}]}}]}
                    delta = {'tool_calls':[{'index':0,'id':'call-'+str(ordinal),'type':'function',
                        'function':{'name':name,'arguments':json.dumps(arguments)}}]}
                    finish = 'tool_calls'
                if workload.scenario.startswith('mixed_') and ordinal==0:
                    arguments={'todos':[{'content':'Verify native gateway','status':'completed'}]}
                    direct={'index':0,'id':'direct-todo','type':'function',
                        'function':{'name':'todo_write','arguments':json.dumps(arguments)}}
                    gateway={'index':1,'id':'gateway-todo','type':'function',
                        'function':{'name':'execute_tools','arguments':json.dumps({
                            'calls':[{'name':'todo_write','call_id':'nested-todo','arguments':arguments}]})}}
                    calls=[direct,gateway] if workload.scenario=='mixed_direct_first' else [gateway,direct]
                    for index,call in enumerate(calls):call['index']=index
                    delta,finish={'tool_calls':calls},'tool_calls'
                data = {'choices': [{'delta': delta, 'finish_reason': finish}],
                        'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
                wire = ('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    async def run(self, arm):
        from lib.agent_core.abort_signal import AbortSignal
        from lib.identity import PrincipalContext
        from lib.model_routing.dispatch_adapter import mint_routed_slot_group, dispose_routed_slot_group
        from lib.model_routing.repository import OwnerBoundary
        from lib.tasks_pkg.native_invocation import NativePreparedInvocation
        from lib.tasks_pkg.orchestrator._run import execute_task_invocation, ROOT_INVOCATION_POLICY
        from lib.turn_lifecycle import create_turn_pair, bind_task, get_turn, get_attempt
        from tests.model_routing.test_native_route import Repository, selection
        if arm not in {'python', 'native'}:
            raise ValueError('unknown root arm')
        task_id, conv_id = uuid.uuid4().hex, uuid.uuid4().hex
        settings = {'model': 'alpha', 'maxTokens': 64, 'temperature': .25,
                    'thinkingEnabled': False, 'memoryEnabled': False, 'projectEnabled': False,
                    'searchMode': 'off', 'tools': {'programmaticCalling': 'off'}}
        start_cpu, start_wall = self.cpu_clock(), time.perf_counter_ns()
        pair = create_turn_pair(conv_id, command_id=conv_id, input_projection={'content': 'question'},
            config=settings, user_id=1, conversation_defaults={'allowCreate': True, 'title': 'Root fixture', 'settings': {}})
        bind_task(pair['attempt']['attemptId'], task_id, user_id=1)
        task = dict(id=task_id, convId=conv_id, _turnId=pair['turn']['turnId'],
            _attemptId=pair['attempt']['attemptId'], _userId=1, status='running', aborted=False,
            content='', thinking='', toolRounds=[], segments=[], config=dict(settings),
            created_at=time.time(), abort_event=AbortSignal(), content_lock=threading.Lock(),
            events=[], events_lock=threading.Lock(), _attended=True, _dispatch_heartbeat=0., _t_last_event=0.,
            messages=[{'role': 'user', 'content': 'question'}])
        repo = Repository()
        repo.boundary = OwnerBoundary.create(1)
        repo.document['connections'][1]['enabled'] = False
        repo.document['connections'][0].update(base_url=f'http://127.0.0.1:{self.server.server_port}/v1',
            inference_profile={'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                'can_disable': True, 'supported_efforts': ['none', 'high'], 'disabled_effort': 'none', 'default_effort': 'high'}})
        before = len(self.bodies)
        self.active_start = before
        group = mint_routed_slot_group(repo, repo.boundary, selection(), owner_tag='task:' + task_id)
        try:
            task['_model_routing_group'], task['_pinned_provider_id'] = group, group.pin_id
            if arm == 'native':
                from tests.support.native_process_probe import config
                from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
                profile = config('ignored', task_id)
                for key in ({'version', 'type', 'owner_user_id', 'task_id', 'messages', 'tools',
                             'rounds', 'deadline_ms', 'tool_calls', 'event_bytes', 'output_bytes',
                             'request_bytes', 'retained_bytes', 'feedback_bytes', 'tool_frame_bytes'} | ROUTE_OVERRIDE_FIELDS):
                    profile.pop(key, None)
                profile['epoch'] = task_id
                profile['approval_control'] = dict(receipts=self.budget.frame_bytes,
                    identity_bytes=self.budget.frame_bytes)
                profile['terminal_persistence'] = dict(operation_id='commit-' + task_id,
                    attempts=2, ack_ms=2000, frame_bytes=self.budget.frame_bytes)
                task['_nativePreparedInvocation'] = NativePreparedInvocation(host=self.host,
                    loop=asyncio.get_running_loop(), principal=PrincipalContext.user(subject_id='root-fixture', owner_user_id=1),
                    repository=repo, selection=selection(), configuration=profile, budget=self.budget,
                    executor=self.executor, working_directory=self.directory, pending_calls=8,
                    pending_bytes=self.budget.frame_bytes, resource_policy=self.resource_policy)
            await asyncio.to_thread(execute_task_invocation, task, policy=ROOT_INVOCATION_POLICY)
        finally:
            dispose_routed_slot_group(group)
        clocks = {'wall_ns': time.perf_counter_ns() - start_wall, 'cpu_ns': self.cpu_clock() - start_cpu}
        turn = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
        attempt = get_attempt(pair['attempt']['attemptId'], user_id=1)
        replay = self.storage.query('task_results.replay_get', {'key': task_id, 'user_id': 1})
        assert task['status'] == 'done', task.get('error')
        assert turn['status'] == attempt['status'] == 'completed'
        assert turn['projection']['content'] == 'durable fixture answer'
        assert replay['status'] == 'done'
        expected = 3 if self.scenario=='discovery_execute' else 2 if self.scenario.startswith('mixed_') else 1
        assert len(self.bodies) == before + expected
        if self.scenario!='text':
            assert task.get('_todos') and task['_todos'][0]['status']=='completed', task.get('toolRounds')
        if self.scenario.startswith('mixed_'):
            assert task['_todoState']['update_count']==1
            contents=[m.get('content','') for m in self.bodies[-1]['messages'] if m['role']=='tool']
            assert any('"status":"delegated"' in content for content in contents), contents
        assert not self.host._clients
        from lib.tasks_pkg.event_delivery import retained_snapshot_bytes
        retained = retained_snapshot_bytes(*(task.get(key) for key in (
            'messages', 'content', 'thinking', 'events', 'segments', 'toolRounds')))
        return dict(arm=arm, clocks=clocks, retained_task_bytes=retained, requests=self.bodies[before:], status=task['status'],
                    content=turn['projection']['content'], usage=turn['projection'].get('usage'),
                    retained_messages=task.get('messages'))

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
