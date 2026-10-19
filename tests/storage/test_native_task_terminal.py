"""Native terminal ACK through real application Turn/task/replay authorities."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tarfile
import threading
import time
import uuid

import pytest

from lib.agent_core.abort_signal import AbortSignal
from lib.agent_core.native_process import TerminalRequest
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy
from tests.storage import test_storage_application_contract as application_contract


pytestmark = pytest.mark.api
application_storage = application_contract.application_storage


@pytest.fixture
def application_storage_owner_id(request):
    # Paid tests use the first registered user, distinct from local owner 1.
    params = getattr(getattr(request.node, 'callspec', None), 'params', {})
    return 2 if params.get('outcome') == 'native_cost' else 1


@pytest.mark.parametrize('commit_debt', [False, True])
def test_native_terminal_ack_requires_owned_turn_task_and_replay_commit(
    application_storage, monkeypatch, commit_debt,
):
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn, get_attempt
    from lib.tasks_pkg.manager._events import reset_task_text
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    from lib.tasks_pkg.manager import _persist as persistence_owner
    from lib.tasks_pkg import native_terminal

    conv_id, task_id = f'native-{uuid.uuid4().hex}', f'native-{uuid.uuid4().hex}'
    pair = create_turn_pair(
        conv_id, command_id=f'create-{conv_id}',
        input_projection={'content': 'question'}, config={'model': 'fixture-model'}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'Native terminal', 'settings': {}},
    )
    attempt_id = pair['attempt']['attemptId']
    bind_task(attempt_id, task_id, user_id=1)
    task = {
        'id': task_id, 'convId': conv_id, '_attemptId': attempt_id, '_userId': 1,
        'status': 'running', 'content': '', 'thinking': '', 'toolRounds': [],
        'segments': [], 'model': 'fixture-model', 'config': {'model': 'fixture-model'},
        'created_at': time.time(), 'abort_event': threading.Event(),
        'content_lock': threading.Lock(),
    }
    assert record_task_event(task, {'type': 'phase', 'phase': 'text'})
    projections = []

    def project(owner_task, candidate):
        projections.append(candidate)
        reset_task_text(owner_task, content=candidate.event['result']['text'])
        stamp_chat_task_terminal(owner_task, status='done', finish_reason='stop')
        terminal = {'type': 'done', 'finishReason': 'stop'}
        assert record_task_event(owner_task, terminal, task_event={
            'task_id': task_id, 'sequence': 1, 'event': terminal,
        }) == 'carried'

    writes = []
    canonical_persist = native_terminal._persist
    canonical_upsert = persistence_owner._upsert_task_row
    checkpoint_attempts = []

    def upsert(*args, **kwargs):
        checkpoint_attempts.append(True)
        if commit_debt and len(checkpoint_attempts) == 1:
            raise OSError('fixture task-result commit unavailable')
        return canonical_upsert(*args, **kwargs)

    monkeypatch.setattr(persistence_owner, '_upsert_task_row', upsert)

    def persist(owner_task):
        writes.append(owner_task['id'])
        return canonical_persist(owner_task)

    monkeypatch.setattr(native_terminal, '_persist', persist)
    principal = PrincipalContext.user(subject_id='native-terminal-contract', owner_user_id=1)
    candidate = TerminalRequest(
        principal, task_id, f'commit-{task_id}', 1,
        json.dumps({'type': 'done', 'status': 'done', 'result': {'text': 'native answer'}}).encode(),
        b'{"invariants_satisfied":true}',
    )

    async def run(executor):
        from dataclasses import replace
        policy = NativeTaskTerminalPolicy(
            principal=principal, task=task, operation_id=candidate.operation_id,
            executor=executor, project=project, candidate_bytes=4096,
        )
        if commit_debt:
            assert await policy.persist(candidate) == 'retry'
            assert task['_terminalPersistencePending']
        assert await policy.persist(replace(candidate, attempt=2 if commit_debt else 1)) == 'accepted'
        assert await policy.persist(replace(candidate, attempt=3)) == 'accepted'

    with ThreadPoolExecutor(max_workers=1) as executor:
        asyncio.run(run(executor))
    assert len(projections) == 1
    assert len(writes) == (2 if commit_debt else 1)
    assert not task.get('_terminalPersistencePending')
    assert task['content'] == 'native answer'  # retained through native publication
    turn = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
    assert turn['status'] == 'completed'
    assert turn['projection']['content'] == 'native answer'
    assert get_attempt(attempt_id, user_id=1)['status'] == 'completed'
    result = application_storage.query('task_results.replay_get', {'key': task_id, 'user_id': 1})
    assert result['status'] == 'done'
    assert application_storage.query('task_results.replay_get', {'key': task_id, 'user_id': 2}) is None
    events = application_storage.query('event.list', {'task_id': task_id, 'after': 0, 'limit': 10})
    assert [row['sequence'] for row in events] == [1]
    assert events[0]['event']['type'] == 'done'


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('outcome', ['done', 'error', 'aborted', 'text_fault', 'long_text',
                                     'approved_write','denied_write','approval_fault','gateway_write','gateway_rounds','anthropic_gateway','anthropic_image','chat_image','image_without_admission','image_feedback_overflow','anthropic_schema','responses_gateway','responses_preparation','preparation_fault','native_context','native_context_refusal','native_context_deadline','native_context_inbox','responses_image','absolute_deadline','native_model_account','native_cost'])
def test_real_native_route_commits_application_terminal(application_storage, monkeypatch, tmp_path, outcome, streaming, canonical_approval=None, application_template=False, application_catalog=False, application_account=False, application_counter=False, application_prepared=False, after_tool_error=False, source_footer=False, issued_route=False, application_root=False, root_stop=False):
    """Rust success, stream failure and cancellation reach the actual authority."""
    archive_path = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive_path:
        pytest.skip('explicit verified native archive required')
    if outcome in {'text_fault', 'long_text','approved_write','denied_write','approval_fault','gateway_write','gateway_rounds','anthropic_gateway','anthropic_image','chat_image','image_without_admission','image_feedback_overflow','anthropic_schema','responses_gateway','responses_preparation','preparation_fault','native_context','native_context_refusal','native_context_deadline','native_context_inbox','responses_image'} and not streaming:
        pytest.skip('text fault requires streaming composition')
    expected_outcome = {'text_fault':'error','long_text':'done','approved_write':'done',
                        'denied_write':'done','approval_fault':'error','gateway_write':'done','gateway_rounds':'done','anthropic_gateway':'done','anthropic_image':'done','chat_image':'done','image_without_admission':'error','image_feedback_overflow':'error','anthropic_schema':'done','responses_gateway':'done','responses_preparation':'done','preparation_fault':'error','native_context':'done','native_context_refusal':'error','native_context_deadline':'aborted','native_context_inbox':'done','responses_image':'done','absolute_deadline':'aborted','native_model_account':'done','native_cost':'done'}.get(outcome,outcome)
    schema_mode=outcome=='anthropic_schema'
    if after_tool_error:expected_outcome='error'
    if root_stop:expected_outcome='aborted'
    output_schema={'type':'object','properties':{'answer':{'type':'string'}},'required':['answer'],'additionalProperties':False}
    answer = 'Rust durable answer' if outcome != 'long_text' else '"\\\n😀中文' * 500
    if schema_mode:
        answer=json.dumps({'answer':answer})
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from lib.agent_core.native_process import Budget, NativeProcessClient
    from lib.agent_core.native_process import NativeProcessError
    from lib.model_routing.native_route import compile_native_route
    from lib.model_routing.repository import OwnerBoundary
    from lib.tasks_pkg.native_execution import run_native_task
    from lib.tasks_pkg import native_execution
    from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn, get_attempt
    from lib.tasks_pkg.manager._events import reset_task_text
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    from lib.error_envelope import normalize_envelope
    from scripts.build_native_runtime import verify
    from tests.model_routing.test_native_route import Repository, selection
    from tests.support.native_process_probe import config

    expected_application_root = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_APPLICATION_ROOT')
    if expected_application_root:
        assert Path(native_execution.__file__).resolve() == (
            Path(expected_application_root) / 'lib/tasks_pkg/native_execution.py'
        ).resolve()

    archive = Path(archive_path)
    manifest = verify(archive)
    binary_dir = tmp_path / 'native'
    binary_dir.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(binary_dir, filter='data')
    owner_id = 1
    if outcome == 'native_cost':
        from lib.billing import users
        wallet = users.create_user(uuid.uuid4().hex+'@native-terminal.example')
        owner_id = wallet.owner_user_id
    task_id, conv_id = f'native-{uuid.uuid4().hex}', f'native-{uuid.uuid4().hex}'
    pair = create_turn_pair(
        conv_id, command_id=f'create-{conv_id}', input_projection={'content': 'question'},
        config={'model': 'fixture-model'}, user_id=owner_id,
        conversation_defaults={'allowCreate': True, 'title': 'Rust terminal', 'settings': {}},
    )
    bind_task(pair['attempt']['attemptId'], task_id, user_id=owner_id)
    task = dict(id=task_id, convId=conv_id, _turnId=pair['turn']['turnId'], _attemptId=pair['attempt']['attemptId'],
                _userId=owner_id, status='running', content='', thinking='', toolRounds=[],
                segments=[], config={}, created_at=time.time(), abort_event=AbortSignal())
    default_text = streaming and outcome in {'done', 'error', 'aborted', 'native_cost', 'absolute_deadline'}
    post_commit_snapshot={}
    if application_prepared:
        from lib.tasks_pkg.orchestrator import _post_commit
        complete=_post_commit.finish_committed_task
        def observe_completion(owner_task,**kwargs):
            post_commit_snapshot.update(owner_task)
            complete(owner_task,**kwargs,commit_round=lambda *a,**k:None,
                consolidate_profile=lambda *a,**k:None)
            assert owner_task['messages'] is None and owner_task['toolRounds'] in (None,[])
        monkeypatch.setattr(_post_commit,'finish_committed_task',observe_completion)
    task.update(content_lock=threading.Lock(), events=[], events_lock=threading.Lock(),
                _attended=True, _dispatch_heartbeat=0.0, _t_last_event=0.0)
    if application_template:
        task['config'].update(maxTokens=64,temperature=.25,thinkingDepth='high')
        if schema_mode:
            task['config']['responseFormat']={'type':'json_schema','json_schema':{
                'name':'answer','schema':output_schema,'strict':True}}
    assert record_task_event(task, {'type': 'phase', 'phase': 'text'})
    if outcome=='native_cost':
        from lib.billing import deposit, reserve
        wallet_owner=wallet.id
        deposit(wallet_owner,1000,ref_id='initial-'+task_id)
        reserve(wallet_owner,100,ref_id=task_id)
        task.update(_billing_reservation_micro=100,_billingUserId=wallet_owner)
    seen, calls, projections, text_observations = [], [], [], []
    checkpoint_observations=[]
    progress_observations=[]
    approval_decisions=[]
    application_prompts=[]
    if canonical_approval is not None:
        from lib.tasks_pkg.tool_dispatch import _approval
        from lib.tasks_pkg.approval import request_write_approval, resolve_write_approval
        def actual_confirmation(approval_id, *, on_registered, timeout, **kwargs):
            application_prompts.append(approval_id)
            assert canonical_approval != 'auto'
            def registered():
                on_registered()
                assert not resolve_write_approval(approval_id, True, owner_user_id=owner_id+1)
                assert resolve_write_approval(approval_id, canonical_approval=='manual', owner_user_id=owner_id)
            return request_write_approval(approval_id, timeout=2, on_registered=registered, **kwargs)
        monkeypatch.setattr(_approval,'request_write_approval',actual_confirmation)
    write_target=tmp_path/'approved-native-tool.txt'
    image_mode=outcome in {'responses_image','anthropic_image','chat_image','image_without_admission','image_feedback_overflow'}
    media_admitted=image_mode and outcome!='image_without_admission'
    responses_mode = outcome in {'native_context','native_context_inbox','native_context_refusal','native_context_deadline','preparation_fault','responses_preparation','responses_gateway','responses_image'}
    anthropic_mode = outcome in {'anthropic_gateway','anthropic_image','anthropic_schema'}
    gateway_mode = outcome in {'native_context','native_context_inbox','native_context_refusal','native_context_deadline','preparation_fault','responses_preparation','responses_gateway','responses_image','gateway_write','gateway_rounds','anthropic_gateway','anthropic_image','chat_image','image_without_admission','image_feedback_overflow','anthropic_schema'}
    multiple_rounds = outcome == 'gateway_rounds'
    second_target=tmp_path/'second-native-tool.txt'
    tool_name = 'write_file' if gateway_mode else 'lookup'
    tool_args = {'path':str(write_target),'content':'registered native application write'} if gateway_mode else {}
    if application_root:tool_args['description']='Native application verification'
    if image_mode:
        import base64
        image_path=tmp_path/'native-image.png'
        image_path.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
        tool_name='inspect_image';tool_args={'path':str(image_path)}
        # This hermetic provider explicitly declares vision capability.
        from lib.tasks_pkg.tool_dispatch import _pipeline
        monkeypatch.setattr(_pipeline,'model_supports_vision',lambda _model:True)
    text_projected = threading.Event()
    provider_entered, provider_release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == ('/v1/messages' if anthropic_mode else '/v1/responses' if responses_mode else '/v1/chat/completions')
            if anthropic_mode:
                assert self.headers['x-api-key']=='private-fixture-token'
                assert self.headers['anthropic-version']=='2023-06-01'
            assert self.headers['Authorization'] == 'Bearer private-fixture-token'
            assert body['model'] == 'alpha-wire-a'
            if application_template:
                assert body['temperature']==.25
                if anthropic_mode:
                    assert body['output_config']['effort']=='high'
                elif responses_mode:
                    assert body['reasoning']['effort']=='high'
                else:
                    assert body['reasoning_effort']=='high'
            if outcome=='responses_preparation':
                assert body['input'][-1]['role']=='developer'
                assert body['input'][-1]['content'][0]['text']==f'prepared round {len(seen)}'
                assert body['input'][0]['content'][0]['text']=='compacted input'
            if schema_mode:
                assert body['output_config']['format']=={'type':'json_schema','schema':output_schema}
                assert body['output_config']['effort']=='high'
                assert 'response_format' not in body
            if outcome=='native_context_inbox':
                assert any('actual operator steer' in str(row) for row in body['input'])
            seen.append(body)
            if after_tool_error and len(seen)==2:
                self.send_response(500);self.send_header('Content-Length','0');self.end_headers();return
            provider_entered.set()
            if outcome == 'aborted':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                if streaming:
                    self.wfile.write(b'data: {"choices":[{"delta":{"reasoning":"thinking","content":"partial"}}]}\n\n')
                    self.wfile.flush()
                provider_release.wait(10)
                return
            delta = {'content': answer}
            finish = 'stop'
            if len(seen) == 1 or (multiple_rounds and len(seen)==2):
                arguments=dict(tool_args,path=str(second_target)) if len(seen)==2 else tool_args
                delta = {'tool_calls': [{'index': 0, 'id': 'call', 'type': 'function',
                                        'function': {'name': tool_name, 'arguments': json.dumps(arguments)}}]}
                finish = 'tool_calls'
            else:
                if gateway_mode:
                    evidence=body['messages'][-1]['content'][0]['content'] if anthropic_mode else next(row['output'] for row in reversed(body['input']) if row.get('type')=='function_call_output') if responses_mode else body['messages'][-1]['content']
                    if image_mode:
                        assert isinstance(evidence,list)
                        from lib.image_mime import sniff_image_mime
                        if anthropic_mode:
                            assert evidence[0]['type']=='image'
                            data=evidence[0]['source']['data'];media_type=evidence[0]['source']['media_type']
                        else:
                            assert evidence[0]['type']==('input_image' if responses_mode else 'image_url')
                            url=evidence[0]['image_url'] if responses_mode else evidence[0]['image_url']['url']
                            header,data=url.split(',',1)
                            media_type=header.split(';',1)[0].removeprefix('data:')
                        assert data and media_type==sniff_image_mime(base64.b64decode(data))
                        assert evidence[-1]['type']==('input_text' if responses_mode else 'text')
                    elif canonical_approval=='denied':
                        assert evidence=='Tool call denied by owner approval policy'
                    else:
                        assert 'File created:' in evidence
                else:
                    assert body['messages'][-1]['content'] == (
                        'Tool call denied by owner approval policy' if outcome=='denied_write' else 'tool evidence')
            wire = ('data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]})
                    + '\n\ndata: [DONE]\n\n').encode()
            if streaming and len(seen) == 1:
                prefix = b'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n\n'
                wire = prefix + wire
            if anthropic_mode:
                events=[]
                if len(seen)==1:
                    events.append({'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':'thinking','signature':'opaque'}})
                    events.append({'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'call','name':tool_name,'input':tool_args}})
                else:
                    assert body['messages'][-2]['content'][0]['signature']=='opaque'
                    events.append({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':answer}})
                events.extend([{'type':'message_delta','delta':{'stop_reason':'tool_use' if len(seen)==1 else 'end_turn'}},{'type':'message_stop'}])
                wire=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
            if responses_mode:
                assert body['store'] is False
                assert body['reasoning']['context']=='all_turns'
                if len(seen)==1:
                    item={'type':'function_call','id':'item','call_id':'call','name':tool_name,'arguments':json.dumps(tool_args)}
                    events=[{'type':'response.reasoning_summary_text.delta','delta':'thinking'},
                            {'type':'response.output_item.added','output_index':0,'item':item},
                            {'type':'response.completed','response':{'output':[{'type':'reasoning','id':'reason','encrypted_content':'opaque'},item]}}]
                else:
                    assert [row.get('encrypted_content') for row in body['input'] if row.get('type')=='reasoning']==['opaque']
                    events=[{'type':'response.completed','response':{'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':answer}]}]}}]
                if outcome == 'native_context':
                    events[-1]['response']['usage'] = {'input_tokens': 760, 'output_tokens': 10, 'total_tokens': 770}
                wire=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
            if outcome in {'native_model_account','native_cost'}:
                wire = ('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}],
                    'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
                if streaming and len(seen) == 1:
                    wire = b'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n\n' + wire
            if outcome == 'error':
                wire = (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                        if streaming else b'') + b'data: [DONE]\n\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    def project(owner_task, candidate):
        if outcome == 'native_context':
            assert checkpoint_observations[-1] == ('seal', candidate.attempt)
        projections.append(candidate)
        event = candidate.event
        assert event['status'] == expected_outcome, event
        if outcome=='native_context_deadline':
            assert slow_entered.is_set() and slow_exited.is_set()
        if outcome in {'preparation_fault','native_context_refusal'}:
            assert event['error']['kind']=='round_preparation_failed'
        from lib.tasks_pkg.native_terminal import project_native_terminal
        project_native_terminal(owner_task, candidate)

    def project_text(owner_task, delta):
        if outcome == 'text_fault':
            raise OSError('fixture text receiver unavailable')
        field = 'content' if delta.channel == 'content' else 'thinking'
        owner_task[field] = owner_task.get(field, '') + delta.text
        owner_task['_contentEpoch'] = int(owner_task.get('_contentEpoch') or 0) + 1
        event = {'type': 'delta', field: delta.text}
        from lib.tasks_pkg.manager import append_event
        append_event(owner_task, event)
        # The canonical publisher owns coalescing and its event sequence.
        delivery = None
        text_observations.append(delta)
        assert owner_task['status'] == 'running'
        if root_stop:owner_task['abort_event'].set()
        if delivery == 'carried':
            visible = get_turn(conv_id, pair['turn']['turnId'], user_id=owner_id)
            assert visible['status'] == 'running'
            assert visible['projection'][field] == owner_task[field]
        text_projected.set()

    if default_text:
        from lib.tasks_pkg import manager
        canonical_append = manager.append_event
        def observe_default_text(owner_task, event):
            result = canonical_append(owner_task, event)
            if event.get('type') == 'delta':
                text_observations.append(dict(event))
                assert owner_task['status'] == 'running'
                assert int(owner_task.get('_contentEpoch') or 0) == 0
                visible = get_turn(conv_id, pair['turn']['turnId'], user_id=owner_id)
                assert visible['status'] == 'running'
                # Ordinary delivery may coalesce durable text between structural
                # boundaries. The producer ACK is not a durable projection ACK.
                assert owner_task['content'].startswith(visible['projection']['content'])
                assert owner_task['thinking'].startswith(visible['projection']['thinking'])
                assert any(row.get('type') == 'delta' for row in owner_task['events'])
                text_projected.set()
            return result
        monkeypatch.setattr(manager, 'append_event', observe_default_text)

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    principal = PrincipalContext.user(subject_id='native-terminal-e2e', owner_user_id=owner_id)
    repo = Repository()
    repo.document['connections'][1]['enabled']=False
    repo.boundary = OwnerBoundary.create(owner_id)
    repo.document['connections'][0]['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
    if anthropic_mode:
        repo.document['connections'][0]['protocol']='anthropic'
    if responses_mode:
        repo.document['connections'][0].update(protocol='openai_responses',inference_profile={'features':{'responses_reasoning_replay':'supported'}})
    if application_template:
        profile=repo.document['connections'][0].setdefault('inference_profile',{})
        profile['reasoning']={'support':'supported','wire_format':'reasoning_effort','can_disable':True,
            'supported_efforts':['none','high'],'disabled_effort':'none','default_effort':'high'}
        if application_template=='unsupported':
            profile['reasoning']['supported_efforts']=['none']
            profile['reasoning']['default_effort']='none'
    if outcome=='native_context_refusal':
        repo.document['offerings'][0]['context_window']=2048
        repo.document['deployments'][0]['max_output_tokens']=32
    route = None if application_prepared else compile_native_route(repo, repo.boundary, selection())
    if outcome == 'absolute_deadline':
        task['config']['executionLimits'] = dict(max_tokens=None, max_tool_calls=None, deadline=0)
    settings = config('ignored', task_id)
    if default_text:
        settings['text_progress'] = True
    for key in ('endpoint', 'headers', 'template'):
        settings.pop(key)
    settings.update(owner_user_id=owner_id, terminal_persistence={
        'operation_id': f'commit-{task_id}', 'ack_ms': 5000, 'attempts': 2, 'frame_bytes': 65536,
    })
    class Policy:
        async def decide(self,request):
            assert request.principal.owner_user_id==owner_id and request.call_id=='call'
            expected_args=dict(tool_args,path=str(second_target)) if approval_decisions else tool_args
            assert request.arguments==expected_args and request.name==tool_name
            approval_decisions.append(request.call_id)
            if outcome=='approval_fault':raise OSError('fixture policy failure')
            return 'denied' if outcome=='denied_write' else 'auto_write_mode'
    write_mode=outcome in {'native_context','native_context_inbox','native_context_refusal','native_context_deadline','preparation_fault','responses_preparation','responses_gateway','approved_write','denied_write','approval_fault','gateway_write','gateway_rounds','anthropic_gateway','anthropic_schema'}
    if gateway_mode:
        settings['tools']={'write_file':{'name':'write_file','contractVersion':'tofu.tool-contract/v2','permission':'write','arguments_schema':{'type':'object','properties':{'path':{'type':'string'},'content':{'type':'string'}},'required':['path','content']}}}
    if image_mode:
        settings['tools']={tool_name:{'name':tool_name,'contractVersion':'tofu.tool-contract/v2','permission':'read','arguments_schema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path']}}}
        if media_admitted:
            settings['tool_feedback']='multimodal'
        if outcome=='image_feedback_overflow':
            settings['feedback_bytes']=16
    if outcome=='native_context_refusal':
        settings['tools'][tool_name]['description']='expensive tool schema '*2000
    if write_mode:
        settings['tools'][tool_name]['permission']='write'
        settings['approval_control']={'receipts':8,'identity_bytes':8192}

    preparation_events=[]
    if outcome=='responses_preparation':
        settings['messages'][0]['content']='x'*16000
    class PreparationPolicy:
        async def prepare(self, request):
            if outcome=='preparation_fault':
                raise OSError('fixture preparation failure')
            assert request.principal.require_owner(context='native prepare test')==owner_id
            assert request.messages and request.history_messages==len(request.messages)
            messages=[dict(row) for row in request.messages if row.get('role')!='developer']
            if request.round==0:
                assert messages[0]['content']=='x'*16000
                messages[0]['content']='compacted input'
            messages.append({'role':'developer','content':f'prepared round {request.round}'})
            preparation_events.append(('prepare',request.round,len(request.messages),len(json.dumps(request.messages)),len(json.dumps(messages))))
            return messages
        async def reserve(self, request):
            assert request.messages[-1]['content']==f'prepared round {request.round}'
            preparation_events.append(('reserve',request.round))
            return True
        async def settle(self, result):
            assert result.evidence['kind']=='returned'
            preparation_events.append(('settle',result.request.round))

    if outcome=='native_context_inbox':
        from lib.agent_inbox import enqueue
        assert enqueue(conv_id,'actual operator steer',mode='user-steer',priority='next')
    slow_entered=threading.Event()
    slow_exited=threading.Event()
    if outcome=='native_context_deadline':
        settings['deadline_ms']=1000
        from lib.tasks_pkg.compaction import _prompt_admission
        def slow_admission(*args,**kwargs):
            slow_entered.set()
            assert task['abort_event'].wait(5)
            slow_exited.set()
        monkeypatch.setattr(_prompt_admission,'enforce_dispatch_prompt_limit',slow_admission)

    async def run(executor):
        shared_image=None
        application_scope=None
        client = NativeProcessClient(
            binary=binary_dir / 'bin/tofu-native',
            sha256=manifest['files']['bin/tofu-native']['sha256'],
            protocol_version=manifest['build']['stdio_contract'], principal=principal,
            budget=Budget(1, 16 * 1024 * 1024,
                1<<20 if application_root else 65536,
                1<<20 if application_root else 131072, 5),
        )
        if application_prepared:
            from lib.agent_core.native_image import NativeArchiveImage
            shared_image=NativeArchiveImage(archive=Path(archive_path),
                sha256=hashlib.sha256(Path(archive_path).read_bytes()).hexdigest(),
                application_version=manifest['build']['application_version'],budget=client.budget)
            await client.close()
            from lib.app.app_factory import create_base_app
            from lib.tasks_pkg.native_host import register_native_host
            application=create_base_app(__name__)
            host=register_native_host(application,image=shared_image)
            application_scope=application.test_app()
            await application_scope.__aenter__()
        async def lookup(candidate):
            assert candidate.task_id == task_id
            calls.append(candidate.name)
            if write_mode:
                assert candidate.call_id=='call'
                await asyncio.to_thread(write_target.write_text,'approved temporary application write')
            return 'tool evidence'
        context_policy=None
        if outcome in {'native_context','native_context_inbox','native_context_refusal','native_context_deadline'}:
            from lib.tasks_pkg.native_context import NativeContextPolicy
            class Accounting:
                async def reserve(self, request):
                    assert task['_lastPromptAdmission']['action']=='admit'
                    preparation_events.append(('reserve',request.round))
                    return True
                async def settle(self, result):
                    preparation_events.append(('settle',result.request.round))
            context_policy=NativeContextPolicy(principal=principal,task=task,executor=executor,round_policy=Accounting())
        model_policy = None
        pricing_resolver = None
        if outcome in {'native_model_account','native_cost'}:
            from lib.agent_core.execution_budget import ExecutionBudget
            from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
            task['config']['executionLimits'] = dict(max_tokens=1000000,max_tool_calls=None,deadline=None)
            task['_executionBudget'] = ExecutionBudget(task)
            model_policy = None if application_account else NativeModelAccountPolicy(principal=principal,task=task,executor=executor)
            if outcome=='native_cost':
                from lib.tasks_pkg.native_pricing import NativeRoutePricing
                pricing_resolver=NativeRoutePricing(principal=principal,margin=.125,
                    lookup=lambda *args,**kwargs:dict(input=2.5,output=10.,currency='USD'),
                    rates=lambda owner:7.24)
                model_policy=None
                class Delivery:
                    async def reserve(self,request):return True
                    async def settle(self,result):return None
                context_policy=Delivery()
            settings['model_control'] = {'ack_ms':5000}
            if outcome=='native_cost':settings['model_control']['pricing']=True
            settings['model_counter'] = dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=1048576,text_bytes=1048576,tool_json_bytes=1048576,depth=128,worker_stack_bytes=1048576)
            if application_counter:
                settings['model_counter'].pop('encoding');settings['model_counter'].pop('output_field')
        checkpoint_policy = None
        if outcome == 'native_context':
            settings.update(progress_control=True, no_progress_rounds=2, nonretryable_failure_rounds=0)
            settings['token_admission'] = dict(encoding='cl100k_base', output_field='max_tokens', total_tokens=1000000,
                input_bytes=1048576, text_bytes=1048576, tool_json_bytes=1048576, depth=128,
                actor_records=1, attempt_records=1, identity_bytes=128, worker_stack_bytes=1048576)
            class Checkpoints:
                binding = 'fixture-owned-model-tool-policy'
                async def load(self, request):
                    assert request.task_id == task['id'] and request.cancelled.is_set() is False
                    checkpoint_observations.append(('load', request.round))
                    return None
                async def begin(self, request):
                    checkpoint_observations.append(('begin', request.round))
                async def complete(self, request):
                    assert request.state['next_round'] == 1 and request.state['tool_calls'] == 1
                    assert request.state['model_account']['last_ordinal'] == 1
                    assert request.state['model_account']['policy']['total_tokens'] == 1000000
                    assert request.state['model_account']['tokens'] == 770
                    assert request.state['model_account']['complete'] is True
                    assert request.state['messages'][-1]['role'] == 'tool'
                    assert request.state['owner_user_id'] == owner_id and request.deadline_monotonic_ns > time.monotonic_ns()
                    assert preparation_events == [('reserve', 0), ('settle', 0)]
                    assert write_target.read_text() == 'registered native application write'
                    checkpoint_observations.append(('complete', request.round))
                    return 'continue'
                async def seal(self, request):
                    assert request.task_id == task['id'] and request.binding == self.binding
                    assert request.suspended is False and request.terminal.event['status'] == 'done'
                    assert write_target.read_text() == 'registered native application write'
                    checkpoint_observations.append(('seal', request.terminal.attempt))
                    return 'retry' if request.terminal.attempt == 1 else 'accepted'
            checkpoint_policy = Checkpoints()
            from lib.tasks_pkg.native_progress import NativeGatewayProgress
            original_observe = NativeGatewayProgress.observe
            async def observe_actual(policy, request):
                assert request.task_id == task['id'] and request.tool_calls == 1
                assert preparation_events == [('reserve', 0), ('settle', 0)]
                observed = await original_observe(policy, request)
                assert policy.batch is None
                progress_observations.append((request.round, request.tool_calls, observed['resultEvidenceComplete'], len(observed['evidenceIds'])))
                return observed
            monkeypatch.setattr(NativeGatewayProgress, 'observe', observe_actual)
        gateway = None
        if gateway_mode:
            from lib.tasks_pkg.native_gateway import NativeGateway
            from lib.tasks_pkg.handlers import project as registered_project
            assert registered_project is not None
            definitions=[{'type':'function','function':{'name':tool_name,'description':'Project tool','parameters':settings['tools'][tool_name]['arguments_schema']}}]
            task.update(_turnId=pair['turn']['turnId'],aborted=False,events=[],events_lock=threading.Lock(),_attended=True,
                        _dispatch_heartbeat=0.0,_t_last_event=0.0,
                        _executable_tool_catalog=definitions)
            from lib.tasks_pkg.manager._events import append_event
            append_event(task, {'type':'phase','phase':'text'})
            if not application_prepared:
                gateway=NativeGateway(principal=principal,task=task,executor=executor,
                    configuration={'autoApply':canonical_approval=='auto'},tool_list=definitions,permissions=None if application_catalog else {tool_name:'read' if image_mode else 'write'},
                    project_path=str(tmp_path),project_enabled=True,model='fixture',round_source=None,
                    decision_policy=None if image_mode or canonical_approval is not None else Policy(),pending_calls=8,pending_bytes=8192,multimodal=media_admitted)
            if application_catalog:
                settings.pop('tools')
        try:
            if application_prepared:
                from lib.tasks_pkg.native_invocation import NativePreparedInvocation
                from lib.tasks_pkg.orchestrator._root_agent_loop import RootLoopRequest
                from types import SimpleNamespace
                from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
                task['config'].update(model='alpha',autoApply=True,projectPath=str(tmp_path))
                root=RootLoopRequest(task=task,state=SimpleNamespace(model='alpha'),messages=settings['messages'],
                    tool_list=definitions,all_search_results_text=[],cfg=task['config'],tid=task_id[:8],
                    thinking_depth='high',temperature=.25,max_tokens=64,response_format=task['config'].get('responseFormat'),
                    project_path=str(tmp_path),project_enabled=True,search_enabled=False)
                if source_footer:root.all_search_results_text.append('URL: https://example.test/retrieved')
                resource={key:value for key,value in settings.items() if key not in
                    ({'version','type','owner_user_id','task_id','messages','tools'} | ROUTE_OVERRIDE_FIELDS)}
                from lib.resource_control.runtime import launch_policy
                for key in ('event_bytes','output_bytes','request_bytes','retained_bytes',
                            'feedback_bytes','tool_frame_bytes','tool_calls','rounds','deadline_ms'):
                    resource.pop(key,None)
                invocation=NativePreparedInvocation(host=host,loop=asyncio.get_running_loop(),principal=principal,
                    repository=repo,selection=selection(),configuration=resource,budget=client.budget,
                    executor=executor,working_directory=binary_dir,pending_calls=8,pending_bytes=8192,
                    resource_policy=launch_policy({}),project_terminal=project,
                    project_text=project_text if streaming and not default_text else None)
                if application_root:
                    from lib.tasks_pkg.orchestrator._run import execute_task_invocation, ROOT_INVOCATION_POLICY
                    task['_nativePreparedInvocation']=invocation
                    task['config'].update(projectEnabled=True,memoryEnabled=False)
                    task['messages']=settings['messages']
                    run=asyncio.create_task(asyncio.to_thread(execute_task_invocation,task,
                        policy=ROOT_INVOCATION_POLICY))
                else:
                    run=asyncio.create_task(asyncio.to_thread(invocation.run,root))
            else:
                run = asyncio.create_task(run_native_task(
                    client, route, settings, task=task, executor=executor,
                    project_terminal=project, prepare_template=None if application_template else lambda metadata: {
                        'model': metadata['wire_model'], 'max_tokens': 100000,
                        **({'response_format':{'type':'json_schema','json_schema':{'name':'answer','schema':output_schema,'strict':True}},'effort':'high'} if schema_mode else {}),
                    }, callbacks={} if gateway_mode else {'lookup': lookup}, working_directory=binary_dir,
                    gateway=gateway,
                    round_policy=context_policy or (PreparationPolicy() if outcome in {'responses_preparation','preparation_fault'} else None),
                    checkpoint_policy=checkpoint_policy, model_policy=model_policy, pricing_resolver=pricing_resolver,
                    project_text=project_text if streaming and not default_text else None,
                    approval_policy=Policy() if write_mode and not gateway_mode else None,
                ))
            if outcome == 'aborted':
                async with asyncio.timeout(5):
                    while not provider_entered.is_set():
                        await asyncio.sleep(0.001)
                    if streaming:
                        while not text_projected.is_set() or task.get('content') != 'partial':
                            await asyncio.sleep(0.001)
                run.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await run
            elif root_stop:
                with pytest.raises(asyncio.CancelledError):await run
            elif application_template=='unsupported':
                from lib.llm.reasoning import ReasoningContractError
                with pytest.raises(ReasoningContractError):
                    await run
            elif outcome=='image_without_admission':
                from lib.agent_core.native_process import NativeToolError
                with pytest.raises(NativeToolError) as caught:
                    await run
                assert isinstance(caught.value.__cause__,ValueError)
            elif outcome=='native_context_deadline':
                with pytest.raises(NativeProcessError) as caught:
                    await run
                assert isinstance(caught.value.__cause__,TimeoutError)
                assert slow_entered.is_set() and slow_exited.is_set()
            elif outcome=='native_context_refusal':
                from lib.llm.llm_errors import PromptTooLongError
                with pytest.raises(NativeProcessError) as caught:
                    await run
                assert isinstance(caught.value.__cause__,PromptTooLongError)
            elif outcome in {'text_fault','approval_fault','preparation_fault'}:
                with pytest.raises(NativeProcessError) as caught:
                    await run
                assert isinstance(caught.value.__cause__, OSError)
            else:
                result = await run
                if application_root:
                    assert result is None and task['status']==expected_outcome
                else:
                    assert result.event['status'] == expected_outcome
            assert client.active_runs == 0
        finally:
            await client.close()
            if application_scope is not None:await application_scope.__aexit__(None,None,None)
            if shared_image is not None:
                assert shared_image._closed
                assert host._closed and not host._clients
    issued_group=None
    try:
        if issued_route:
            from lib.model_routing.dispatch_adapter import mint_routed_slot_group
            issued_group=mint_routed_slot_group(repo,repo.boundary,selection(),owner_tag='task:'+task_id)
            task['_model_routing_group']=issued_group
            task['_pinned_provider_id']=issued_group.pin_id
        with ThreadPoolExecutor(max_workers=1) as executor:
            asyncio.run(run(executor))
    finally:
        if issued_group is not None:
            from lib.model_routing.dispatch_adapter import dispose_routed_slot_group
            dispose_routed_slot_group(issued_group)
        provider_release.set()
        server.shutdown()
        thread.join()
        server.server_close()
    if application_template=='unsupported':
        assert not seen and not calls and not projections and not task['toolRounds']
        assert not write_target.exists() and task['status']=='running'
        return
    if expected_outcome=='done' and outcome!='responses_preparation':
        if application_prepared:
            assert post_commit_snapshot and task['messages'] is None and task['toolRounds'] in (None,[])
            task=post_commit_snapshot
        final_snapshots=[event for event in task['events'] if event.get('type')=='messages_snapshot' and event.get('kind')=='state' and event.get('roundNum')=='final']
        assert len(final_snapshots)==1
        assert final_snapshots[0]['messages'][-1]['role']=='assistant'
    if application_prepared and expected_outcome!='done':
        assert post_commit_snapshot and task['messages'] is None and task['toolRounds'] in (None,[])
        task=post_commit_snapshot
    if outcome in {'done','gateway_write','native_model_account','native_cost'}:
        assert task['_contextBudgetDecision']['contextWindowSource']=='model_routing_v2'
        assert task['_lastPromptAdmission']['action']=='admit'
    if outcome in {'native_model_account','native_cost'}:
        assert task['_executionBudget'].consumption_snapshot() == (10,0,True)
        assert task['_executionBudget'].continuity_snapshot()['pending_model_requests'] == 0
    if outcome in {'native_context','native_context_inbox'}:
        assert preparation_events==[('reserve',0),('settle',0),('reserve',1),('settle',1)]
        assert task['_contextBudgetDecision']['contextWindowSource']=='model_routing_v2'
        assert task['_lastPromptAdmission']['action']=='admit'
    if outcome=='native_context':
        assert progress_observations == [(0, 1, True, 1)]
        assert checkpoint_observations == [('load', 0), ('begin', 0), ('complete', 0), ('begin', 1), ('seal', 1), ('seal', 2)]
    if outcome=='native_context_inbox':
        assert not task.get('_steer_inject_pending')
        assert len(task.get('_userSteerInjects') or [])==1
    if outcome=='native_context_refusal':
        assert not preparation_events
        assert task['_lastPromptAdmission']['action']=='refuse_tool_surface'
        assert task['_contextBudgetDecision']['contextWindowTokens']==2048
    if outcome=='responses_preparation':
        assert [row[:2] for row in preparation_events]==[('prepare',0),('reserve',0),('settle',0),('prepare',1),('reserve',1),('settle',1)]
        print('NATIVE_PREPARATION_MEASUREMENT '+json.dumps(preparation_events))
    assert len(seen) == (2 if after_tool_error else 0 if outcome in {'preparation_fault','native_context_refusal','native_context_deadline','absolute_deadline'} else 3 if multiple_rounds else 2 if expected_outcome == 'done' else 1)
    assert calls == (['lookup'] if expected_outcome == 'done' and outcome != 'denied_write' and not gateway_mode else [])
    assert len(projections) == 1 and projections[0].event['status'] == expected_outcome
    boundaries = [event for event in task['events'] if event.get('type') in {'round_start','round_end'}]
    starts = [event['roundNum'] for event in boundaries if event['type']=='round_start']
    ends = [event['roundNum'] for event in boundaries if event['type']=='round_end']
    assert starts == list(range(len(starts)))
    cancelled_tail = expected_outcome == 'aborted' and bool(starts)
    # Stop withdraws the final presentation frame, but must still commit the
    # interrupted Turn and joined terminal ACK checked below.
    assert ends == (starts[:-1] if cancelled_tail else starts)
    expected_boundaries = [(kind, number) for number in ends
                           for kind in ('round_start', 'round_end')]
    if cancelled_tail:
        expected_boundaries.append(('round_start', starts[-1]))
    assert [(event['type'],event['roundNum']) for event in boundaries] == expected_boundaries
    if starts and not cancelled_tail:
        assert boundaries[-1]['reason'] == ('stop' if expected_outcome=='done' else expected_outcome)
    assert len(repo.secret_calls) == 1 + int(issued_route)
    result = application_storage.query('task_results.replay_get', {'key': task_id, 'user_id': owner_id})
    assert result['status'] == expected_outcome
    if after_tool_error:
        assert len(seen)==2 and task['_messages_include_current_run']
        assert task['messages'][-1]['role']=='tool'
        history=projections[0].event['execution']['messages']
        assert history==task['messages']
        assert len(history)>=3 if application_root else len(history)==3
    if outcome=='native_cost':
        assert len(task['apiRounds'])==2
        assert [row['nativeCredits']['total'] for row in task['apiRounds']]==[31,31]
        assert task['cost']['costUsd']==.0001
    turn = get_turn(conv_id, pair['turn']['turnId'], user_id=owner_id)
    expected_status = {'done': 'completed', 'error': 'failed', 'aborted': 'interrupted'}[expected_outcome]
    assert turn['status'] == expected_status
    if outcome=='native_cost':
        assert [row['nativeCredits']['total'] for row in turn['projection']['apiRounds']]==[31,31]
        assert turn['projection']['cost']['costUsd']==.0001
        from lib.billing import deposit, reserve, get_balance, list_entries
        from lib.billing.request_flow import settle_task
        stored=application_storage.query('task_results.replay_get',
            {'key':task_id,'user_id':owner_id,'include_metadata':True})
        capsule=stored['metadata']['nativeBillingSettlement']
        assert capsule['reserved_micro']==100 and capsule['componentsMicro']['totalMicro']==62
        monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
        def refuse_repricing(*args,**kwargs):
            raise AssertionError('native wallet must not reprice paid rounds')
        monkeypatch.setattr('lib.billing.compute_request_cost',refuse_repricing)
        import lib.billing as billing
        canonical_settle=billing.settle
        def lost_ack(*args,**kwargs):
            canonical_settle(*args,**kwargs)
            raise OSError('wallet committed before lost acknowledgement')
        monkeypatch.setattr(billing,'settle',lost_ack)
        with pytest.raises(OSError,match='lost acknowledgement'):
            settle_task(task,user_id=wallet_owner,model='alpha',raise_on_error=True)
        assert get_balance(wallet_owner)==938
        monkeypatch.setattr(billing,'settle',canonical_settle)
        first=settle_task(task,user_id=wallet_owner,model='alpha',raise_on_error=True)
        again=settle_task(task,user_id=wallet_owner,model='alpha',raise_on_error=True)
        assert first['cost_micro']==again['cost_micro']==62
        assert get_balance(wallet_owner)==938
        from lib.storage_authority_sql.generated_billing_contract import NATIVE_ADMISSION_KIND
        movements=[entry for entry in list_entries(wallet_owner) if entry.ref_id==task_id]
        assert sorted((entry.kind,entry.amount_micro) for entry in movements)==sorted([
            ('debit',-62),(NATIVE_ADMISSION_KIND,0),('reserve',-100),('reserve_release',100)])
    assert get_attempt(pair['attempt']['attemptId'], user_id=owner_id)['status'] == expected_status
    assert not task.get('_terminalPersistencePending')
    if expected_outcome == 'done':
        expected_answer=answer+('\n\n---\n**来源 / Sources**（本轮检索所用，供核对）：\n- https://example.test/retrieved' if source_footer else '')
        assert turn['projection']['content'] == expected_answer
        if source_footer:
            assert projections[0].event['result']['messages'][-1]['content']==answer
    elif expected_outcome == 'error':
        assert task['error']
    else:
        assert task['aborted'] is True
    if outcome in {'preparation_fault','native_context_refusal','native_context_deadline'}:
        assert not approval_decisions and not write_target.exists() and not task.get('toolRounds')
    if root_stop:
        assert task['abort_event'].is_set()
        assert not write_target.exists() and not task['toolRounds']
        assert not turn['projection'].get('toolRounds')
        assert len(seen)==1 and text_observations
    elif canonical_approval is not None:
        assert not approval_decisions
        assert len(application_prompts)==(0 if canonical_approval=='auto' else 1)
        assert write_target.exists()==(canonical_approval!='denied')
        assert len(task['toolRounds'])==1
        assert task['toolRounds'][0]['status']==('rejected' if canonical_approval=='denied' else 'done')
        assert len(turn['projection']['toolRounds'])==1
    elif write_mode and outcome not in {'preparation_fault','native_context_refusal','native_context_deadline'}:
        assert approval_decisions==(['call','call'] if multiple_rounds else ['call'])
        assert write_target.exists()==(outcome in {'approved_write','gateway_write','gateway_rounds','anthropic_gateway','anthropic_schema','responses_gateway','responses_preparation','native_context','native_context_inbox'})
    if gateway_mode and not root_stop and outcome not in {'preparation_fault','native_context_refusal','native_context_deadline'}:
        if image_mode:
            assert not approval_decisions and not write_target.exists()
            assert task['toolRounds'][0].get('toolModelContent')
        elif canonical_approval!='denied':
            assert write_target.read_text()=='registered native application write'
        assert len(task['toolRounds'])==(2 if multiple_rounds else 1)
        assert all(row['status']==('rejected' if canonical_approval=='denied' else 'done') for row in task['toolRounds'])
        if multiple_rounds:
            assert second_target.read_text()=='registered native application write'
            assert [row['llmRound'] for row in task['toolRounds']]==[0,1]
    if outcome == 'absolute_deadline':
        assert not text_observations and not seen and task['finishReason'] == 'execution_deadline_exceeded'
    if streaming and outcome not in {'text_fault','approval_fault','preparation_fault','native_context_refusal','native_context_deadline','absolute_deadline'}:
        assert text_observations
        if not default_text:
            assert [delta.sequence for delta in text_observations] == list(range(1, len(text_observations) + 1))
        if expected_outcome != 'done' and outcome not in {'image_without_admission','image_feedback_overflow'}:
            assert turn['projection']['content'] == (task.get('content') or '' if root_stop else '' if after_tool_error else 'partial')
        if root_stop:
            assert turn['projection']['thinking']==task.get('thinking','')
        elif outcome != 'error':
            assert turn['projection']['thinking'] == ('thinking\n\n' if responses_mode else 'thinking')


@pytest.mark.parametrize('approval_mode',['auto','manual','denied'])
def test_real_native_gateway_uses_application_approval(application_storage,monkeypatch,tmp_path,approval_mode):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        'gateway_write',True,canonical_approval=approval_mode)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_real_native_generation_uses_task_and_selected_profile(application_storage,monkeypatch,tmp_path,protocol_outcome):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,application_template=True)


def test_real_native_generation_refuses_before_provider_dispatch(application_storage,monkeypatch,tmp_path):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        'gateway_write',True,application_template='unsupported')


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_real_native_catalog_uses_prepared_tools(application_storage,monkeypatch,tmp_path,protocol_outcome):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,application_catalog=True)


def test_real_native_model_account_defaults_to_existing_task_budget(application_storage,monkeypatch,tmp_path):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        'native_model_account',False,application_account=True)


def test_real_native_counter_defaults_from_selected_route(application_storage,monkeypatch,tmp_path):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        'native_model_account',False,application_account=True,application_counter=True)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_native_failure_after_tool_keeps_observed_history_and_durable_effect(application_storage,monkeypatch,tmp_path,protocol_outcome):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,
        application_prepared=True,after_tool_error=True)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_gateway'])
def test_native_source_footer_is_committed_with_final_display_text(application_storage,monkeypatch,tmp_path,protocol_outcome):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,
        application_prepared=True,source_footer=True)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_real_prepared_root_request_executes_in_native_runtime(application_storage,monkeypatch,tmp_path,protocol_outcome):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,application_prepared=True)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_real_prepared_native_task_keeps_its_issued_route(application_storage,monkeypatch,tmp_path,protocol_outcome):
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,
        application_prepared=True,issued_route=True)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_real_application_root_selects_native_without_python_rounds(application_storage,monkeypatch,tmp_path,protocol_outcome):
    from lib.tasks_pkg.orchestrator import _run as root_owner
    def refuse_python_rounds(*args,**kwargs):
        raise AssertionError('native root handoff entered the Python model loop')
    monkeypatch.setattr(root_owner,'run_root_agent_loop',refuse_python_rounds)
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,
        application_prepared=True,issued_route=True,application_root=True)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_real_application_root_native_failure_preserves_history_and_tool_effect(application_storage,monkeypatch,tmp_path,protocol_outcome):
    from lib.tasks_pkg.orchestrator import _run as root_owner
    def refuse_python_rounds(*args,**kwargs):
        raise AssertionError('native root failure replayed the Python model loop')
    monkeypatch.setattr(root_owner,'run_root_agent_loop',refuse_python_rounds)
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,
        application_prepared=True,issued_route=True,application_root=True,after_tool_error=True)


@pytest.mark.parametrize('protocol_outcome',['gateway_write','responses_gateway','anthropic_schema'])
def test_real_application_root_native_owner_stop_joins_before_terminal(application_storage,monkeypatch,tmp_path,protocol_outcome):
    from lib.tasks_pkg.orchestrator import _run as root_owner
    def refuse_python_rounds(*args,**kwargs):
        raise AssertionError('native owner stop entered the Python model loop')
    monkeypatch.setattr(root_owner,'run_root_agent_loop',refuse_python_rounds)
    test_real_native_route_commits_application_terminal(application_storage,monkeypatch,tmp_path,
        protocol_outcome,True,canonical_approval='auto',application_template=True,
        application_prepared=True,issued_route=True,application_root=True,root_stop=True)
