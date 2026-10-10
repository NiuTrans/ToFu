"""Pilot fairness, exact oracles, missing evidence, and failure retention."""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from evaluations.programmatic_pilot.runner import config, paired_report, run_task, usage_cost
from evaluations.programmatic_pilot.workloads import canonical, oracle, read_world, workloads

pytestmark = pytest.mark.unit


def test_world_and_oracles_cover_composition_and_negative_controls():
    tasks = workloads()
    assert {t.family for t in tasks} == {
        'data_reduction', 'data_composition', 'semantic_judgment', 'small_result'}
    assert [t.fingerprint for t in tasks] == [t.fingerprint for t in workloads()]
    for t in tasks:
        assert oracle(t, t.expected_json)
        assert not oracle(t, '{}')
        assert not oracle(t, '```json\n' + t.expected_json + '\n```')
        for key in json.loads(t.world_json):
            assert json.loads(read_world(t, 'custom__read_dataset', {'dataset': key})) is not None
        with pytest.raises(ValueError):
            read_world(t, 'run_command', {'command': 'anything'})
        with pytest.raises(ValueError):
            read_world(t, 'custom__read_dataset', {'dataset': key, 'other': True})


def test_bulk_direct_lane_can_reconstruct_complete_source_by_pages():
    from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
    task = workloads()[0]
    rows = json.loads(task.world_json)['ledger']
    reconstructed = []
    for offset in range(0, len(rows) + 50, 50):
        output = read_world(task, 'custom__read_dataset',
                            {'dataset': 'ledger', 'offset': offset, 'limit': 50})
        assert len(output.encode()) < 24_000
        view = json.loads(budget_tool_result_v2('custom__read_dataset', output,
                                              user_id=73, model='kimi-k3'))
        assert view['viewCoverage'] == 'complete'
        assert view['truncated'] is False
        reconstructed.extend(json.loads(output))
    assert reconstructed == rows
    with pytest.raises(ValueError):
        read_world(task, 'custom__read_dataset', {'dataset': 'ledger', 'offset': True})


def test_pair_changes_only_program_availability():
    a, b = config('direct', 'medium'), config('programmatic', 'medium')
    assert a['tools'].pop('programmaticCalling') == 'off'
    assert b['tools'].pop('programmaticCalling') == 'on'
    assert a == b
    assert a['orchestration']['multiAgent'] == 'off'


def test_cost_requires_complete_cache_usage_and_frozen_units():
    card = {'currency': 'USD', 'source': 'https://creator.example/prices',
            'perMillionTokens': {'input': 2, 'cachedInput': 0.2, 'output': 8}}
    usage = {'prompt_tokens': 1000, 'completion_tokens': 100,
             'prompt_tokens_details': {'cached_tokens': 500}}
    assert usage_cost(usage, card) == pytest.approx(0.0019)
    assert usage_cost(usage, None) is None
    assert usage_cost({'prompt_tokens': 1000, 'completion_tokens': 100}, card) is None
    assert usage_cost({**usage, 'prompt_tokens': True}, card) is None
    assert usage_cost({**usage, 'prompt_tokens_details': {'cached_tokens': 1001}}, card) is None
    with pytest.raises(ValueError):
        usage_cost({}, {**card, 'perMillionTokens': {'input': float('nan')}})


def records():
    return [{'taskId': t.task_id, 'arm': arm, 'trial': 0,
             'oraclePassed': True, 'programCount': 0, 'wallMs': 100,
             'costUsd': None} for t in workloads() for arm in ('direct', 'programmatic')]


def test_pair_report_rejects_partial_evidence_and_never_promotes_pilot():
    rows = records()
    report = paired_report(rows, trials=1)
    assert report['releaseClaim'] is False and report['allOraclesPass']
    assert report['costMeasured'] is False
    assert all(p['candidatePrograms'] == 0 for p in report['pairedTrials'])
    for invalid in [rows[:-1], rows + [rows[0]], rows + [{**rows[0], 'taskId': 'foreign'}]]:
        with pytest.raises(ValueError):
            paired_report(invalid, trials=1)


@pytest.mark.parametrize('read,fail', [(True, False), (False, False), (True, True)])
def test_runner_keeps_native_evidence_and_requires_reads(tmp_path, read, fail):
    workload = workloads()[-1]
    aborted, resolutions = [], []
    class Execution:
        async def events_async(self, **kwargs):
            if read:
                yield {'type': 'custom_tool_call', 'callId': 'owned-call',
                       'toolName': 'custom__read_dataset',
                       'arguments': {'dataset': 'status'}}
            if fail:
                raise TimeoutError('provider exception body must not be persisted')
        def resolve_custom_tool_call(self, call_id, content, **kwargs):
            resolutions.append((call_id, content))
            return True
        async def result_async(self, **kwargs):
            return SimpleNamespace(status='done', finish_reason='stop',
                                   content=workload.expected_json)
        def abort(self):
            aborted.append(True)
        def evidence_snapshot(self):
            return {'status': 'done', 'usage': {}, 'apiRounds': [], 'programRuns': []}
    class Runtime:
        def start(self, messages, **kwargs):
            assert kwargs['custom_tools_mode'] == 'exclusive'
            assert not any('program' in m['content'].lower() for m in messages)
            return Execution()
    result = asyncio.run(run_task(Runtime(), workload, 'programmatic', 0,
                                 tmp_path, 'medium', 30, None))
    assert result['oraclePassed'] is (read and not fail)
    assert result['runnerFailure'] == ('TimeoutError' if fail else None)
    assert aborted == ([True] if fail else [])
    assert (tmp_path / 'events.jsonl').exists()
    assert json.loads((tmp_path / 'record.json').read_text()) == result
    assert 'provider exception body' not in (tmp_path / 'record.json').read_text()
    assert bool(resolutions) is read


@pytest.mark.parametrize('arm', ['direct', 'programmatic'])
def test_public_runtime_executes_pilot_client_io_and_program(monkeypatch, tmp_path, arm, provider_probe_url):
    """Only provider transport is simulated; the whole production run executes."""
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    import lib.llm

    workload = workloads()[-1]
    provider_calls = []
    def stream(body, **kwargs):
        provider_calls.append(body)
        if len(provider_calls) == 1:
            if arm == 'programmatic':
                name, arguments = 'execute_program', {'code':
                    'const data = await tools.invoke("custom__read_dataset", '
                    '{dataset:"status"}); return {state:data.state};'}
            else:
                name, arguments = 'custom__read_dataset', {'dataset': 'status'}
            call = {'id': f'pilot-{arm}', 'type': 'function',
                    'function': {'name': name, 'arguments': canonical(arguments)}}
            kwargs['on_tool_call_ready'](call)
            message, finish = {'role': 'assistant', 'content': '', 'tool_calls': [call]}, 'tool_calls'
        else:
            assert len(provider_calls) == 2
            assert any(m.get('role') == 'tool' for m in body['messages'])
            if arm == 'programmatic':
                deliveries = [m for m in body['messages'] if m.get('role') == 'tool']
                assert len(deliveries) == 1
                assert deliveries[0]['tool_call_id'] == 'pilot-programmatic'
                assert json.loads(deliveries[0]['content']) == {'state':'ready'}
            kwargs['on_content'](workload.expected_json)
            message, finish = {'role': 'assistant', 'content': workload.expected_json}, 'stop'
        return ProviderStreamResult(message=message,
            compatibility_finish_reason=finish, provider_finish_reason=finish,
            state=ProviderStreamState.PROVIDER_FINISHED, saw_done=True,
            saw_finish_reason=True, usage={'prompt_tokens': 10,
                'completion_tokens': 5, 'prompt_tokens_details': {'cached_tokens': 0}})
    monkeypatch.setattr(lib.llm, 'stream_chat', stream)
    access = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
        base_url=provider_probe_url))
    with AgentRuntime.local(model_routing=access, max_inflight=1) as runtime:
        record = asyncio.run(run_task(runtime, workload, arm, 0, tmp_path,
                                      'medium', 30, None))
    assert record['runnerFailure'] is None, record
    assert record['oraclePassed'], record
    assert record['leafCallCount'] == 1
    assert record['programCount'] == (1 if arm == 'programmatic' else 0)
    assert len(provider_calls) == 2
    if arm == 'programmatic':
        assert record['programStats'][0]['outcome'] == 'completed'
        assert record['programStats'][0]['tool_calls'] == 1


def test_public_runtime_reuses_an_ordinary_read_without_another_leaf_call(monkeypatch, tmp_path, provider_probe_url):
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from evaluations.programmatic_pilot.workloads import Workload
    import lib.llm

    rows = [{'amount':i+1,'note':'private source '*50} for i in range(9)]
    workload = Workload('reuse','result_reuse','Sum the amounts and return JSON.',
                        canonical({'rows':rows}),canonical({'total':45}))
    bodies = []
    def stream(body, **kwargs):
        bodies.append(body)
        if len(bodies) < 3:
            name, args = ('custom__read_dataset',{'dataset':'rows'}) if len(bodies)==1 else (
                'execute_program',{'code':'const rows=tools.result("custom__read_dataset",{dataset:"rows"}); return {total:rows.reduce((s,r)=>s+r.amount,0)};'})
            call = {'id':f'reuse-{len(bodies)}','type':'function',
                    'function':{'name':name,'arguments':canonical(args)}}
            kwargs['on_tool_call_ready'](call)
            message, finish = {'role':'assistant','content':'','tool_calls':[call]}, 'tool_calls'
        else:
            assert len(bodies)==3
            deliveries = [m for m in body['messages'] if m.get('role')=='tool']
            assert len(deliveries)==2
            assert json.loads(deliveries[-1]['content']) == {'total':45}
            kwargs['on_content'](workload.expected_json)
            message, finish = {'role':'assistant','content':workload.expected_json}, 'stop'
        return ProviderStreamResult(message=message,compatibility_finish_reason=finish,
            provider_finish_reason=finish,state=ProviderStreamState.PROVIDER_FINISHED,
            saw_done=True,saw_finish_reason=True,usage={'prompt_tokens':10,'completion_tokens':5})
    monkeypatch.setattr(lib.llm,'stream_chat',stream)
    access = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(base_url=provider_probe_url))
    with AgentRuntime.local(model_routing=access,max_inflight=1) as runtime:
        record = asyncio.run(run_task(runtime,workload,'programmatic',0,tmp_path,'medium',30,None))
        assert all('_programResultRefs' not in execution._task for execution in runtime._executions.values())
    assert record['runnerFailure'] is None, record
    assert record['oraclePassed'] and record['leafCallCount']==1
    assert record['programCount']==1
    assert record['programStats'][0]['result_reads']==1
    assert record['programStats'][0]['tool_calls']==0


@pytest.fixture
def provider_probe_url():
    """Owned TCP listener satisfies production route reachability preflight."""
    with socket.create_server(('127.0.0.1', 0)) as endpoint:
        yield f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'


class _NativePilotAPI:
    origin = 'http://127.0.0.1:15000'

    def __init__(self, *, arm='programmatic', program=True, foreign=False,
                 reset=False, provider='provider-a'):
        self.calls = []
        self.provider = provider
        self.status = 'running'
        names = ['custom__list_datasets', 'custom__read_dataset']
        if arm == 'programmatic':
            names.append('execute_program')
        if foreign:
            names.append('run_command')
        self.pages = [{
            'events': [
                {'type':'tool_wire_projection', 'seq':1, 'taskId':'native-task',
                 'toolNames':names, 'executableToolCount':2, 'schemaFingerprint':'schema-1'},
                {'type':'custom_tool_call', 'seq':2, 'taskId':'native-task',
                 'callId':'read-1', 'toolName':'custom__read_dataset',
                 'arguments':{'dataset':'status'}},
            ], 'next_cursor':3, 'caught_up':True, 'done':False,
            'cursor':{'reset':reset},
        }, {'events':[], 'next_cursor':5, 'caught_up':True, 'done':True}]
        if program:
            self.pages[1]['events'] = [
                {'type':'program_start', 'seq':3, 'taskId':'native-task',
                 'programCallId':'program-1', 'source':'execute_program',
                 'backend':'local_javascript', 'status':'running'},
                {'type':'program_output', 'seq':4, 'taskId':'native-task',
                 'programCallId':'program-1', 'source':'execute_program',
                 'backend':'local_javascript', 'status':'completed',
                 'stats':{'engine':'quickjs-wasm', 'tool_calls':1}},
            ]

    def call(self, path, body=None):
        self.calls.append((path, body))
        if path == '/api/v4/agent/run':
            return {'task_id':'native-task','provider_id':self.provider}
        if '/events?' in path:
            page = self.pages.pop(0)
            if page['done']:
                self.status = 'done'
            return page
        if path.endswith('/tool_result'):
            return {'resolved':True,'callId':body['call_id'],'taskId':'native-task'}
        if path.endswith('/abort'):
            self.status = 'aborted'
            return {'ok':True}
        if path == '/api/v4/tasks/native-task':
            return {'id':'native-task','status':self.status,'finishReason':'stop',
                    'content':'{"state":"ready"}',
                    'usage':{'prompt_tokens':101,'completion_tokens':7,
                             'prompt_tokens_details':{'cached_tokens':80}},
                    'apiRounds':[{'round':1},{'round':2}]}
        raise AssertionError(path)


def test_native_api_pilot_runs_owned_callbacks_and_preserves_native_program_evidence(tmp_path):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot.runner import run_task
    api = _NativePilotAPI()
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    workload = next(x for x in workloads() if x.task_id == 'small-lookup')
    result = asyncio.run(run_task(runtime, workload, 'programmatic', 0,
                                 tmp_path, 'medium', 10, None))
    assert result['oraclePassed'] is True
    assert result['requiredReadsComplete'] is True
    assert result['programCount'] == 1
    assert result['programStats'] == [{'engine':'quickjs-wasm','tool_calls':1}]
    assert result['usage']['prompt_tokens'] == 101
    assert result['apiRoundCount'] == 2
    assert runtime.in_flight == 0
    admission = api.calls[0][1]
    assert admission['async'] is True
    assert admission['config']['_customToolsMode'] == 'exclusive'
    assert len(admission['config']['_explicitToolSchemas']) == 2
    callbacks = [body for path,body in api.calls if path.endswith('/tool_result')]
    assert callbacks == [{'call_id':'read-1','content':'{"revision":17,"state":"ready"}',
                          'is_error':False}]
    runtime.close()


@pytest.mark.parametrize('reported_count', [32, 2, 31, 33])
def test_native_pilot_wide_catalog_requires_exact_admitted_cardinality(tmp_path, monkeypatch, reported_count):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot import runner
    original_config, original_schemas = runner.config, runner.tool_schemas
    def config(arm, thinking):
        value = original_config(arm, thinking)
        value['tools']['programmaticExposure'] = 'code_api'
        return value
    catalog = original_schemas() + [
        {'type':'function','function':{'name':'custom__unused_' + str(n),
         'description':'Unrelated lookup','parameters':{'type':'object','properties':{}}},
         'execution':{'mode':'client'},'write':False} for n in range(30)]
    monkeypatch.setattr(runner, 'config', config)
    monkeypatch.setattr(runner, 'tool_schemas', lambda workload=None: catalog)
    api = _NativePilotAPI()
    api.pages[0]['events'][0].update(toolNames=['execute_program'], executableToolCount=reported_count)
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    record = asyncio.run(runner.run_task(runtime, workloads()[-1], 'programmatic', 0,
                                       tmp_path, 'medium', 10, None))
    assert len(api.calls[0][1]['tools']) == 32
    assert record['oraclePassed'] is (reported_count == 32)
    assert record['runnerFailure'] == (None if reported_count == 32 else 'ValueError')
    assert runtime.in_flight == 0


@pytest.mark.parametrize('fault', [None, 'direct', 'batch', 'expanded_catalog', 'missing_fingerprint'])
def test_native_code_api_pilot_verifies_gateway_wire_and_full_child_authority(tmp_path, monkeypatch, fault):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot import runner

    original_config = runner.config

    def code_api_config(arm, thinking):
        cfg = original_config(arm, thinking)
        cfg['tools']['programmaticExposure'] = 'code_api'
        return cfg

    monkeypatch.setattr(runner, 'config', code_api_config)
    api = _NativePilotAPI()
    wire = api.pages[0]['events'][0]
    wire['toolNames'] = ['execute_program']
    if fault == 'direct':
        wire['toolNames'] += ['custom__list_datasets', 'custom__read_dataset']
    elif fault == 'batch':
        wire['toolNames'] = ['execute_tools']
    elif fault == 'expanded_catalog':
        wire['executableToolCount'] = 3
    elif fault == 'missing_fingerprint':
        wire['schemaFingerprint'] = ''
    runtime = HTTPRuntime(api, {'creator_id': 'deepseek', 'model_id': 'deepseek-v4-flash'})
    record = asyncio.run(runner.run_task(runtime, workloads()[-1], 'programmatic', 0,
                                        tmp_path, 'medium', 10, None))
    assert len(api.calls[0][1]['tools']) == 2
    if fault is None:
        assert record['runnerFailure'] is None
        assert record['oraclePassed'] is True
        assert record['programCount'] == 1
        assert len([p for p, _ in api.calls if p.endswith('/tool_result')]) == 1
    else:
        assert record['runnerFailure'] == 'ValueError'
        assert not any(p.endswith('/tool_result') for p, _ in api.calls)
        assert any(p.endswith('/abort') for p, _ in api.calls)
    assert runtime.in_flight == 0
    runtime.close()


def test_native_code_api_flag_does_not_change_direct_arm(tmp_path, monkeypatch):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot import runner

    original_config = runner.config

    def code_api_config(arm, thinking):
        cfg = original_config(arm, thinking)
        cfg['tools']['programmaticExposure'] = 'code_api'
        return cfg

    monkeypatch.setattr(runner, 'config', code_api_config)
    api = _NativePilotAPI(arm='direct', program=False)
    runtime = HTTPRuntime(api, {'creator_id': 'deepseek', 'model_id': 'deepseek-v4-flash'})
    record = asyncio.run(runner.run_task(runtime, workloads()[-1], 'direct', 0,
                                        tmp_path, 'medium', 10, None))
    assert record['runnerFailure'] is None
    assert record['oraclePassed'] is True
    assert record['programCount'] == 0
    assert runtime.in_flight == 0
    runtime.close()


@pytest.mark.parametrize('fault', ['foreign', 'reset'])
def test_native_api_pilot_rejects_changed_authority_or_evicted_evidence_and_aborts(tmp_path, fault):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot.runner import run_task
    api = _NativePilotAPI(**{fault:True})
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    result = asyncio.run(run_task(runtime, workloads()[-1], 'programmatic', 0,
                                 tmp_path, 'medium', 10, None))
    assert result['oraclePassed'] is False
    assert result['runnerFailure'] == 'ValueError'
    assert any(path.endswith('/abort') for path,_ in api.calls)
    assert not any(path.endswith('/tool_result') for path,_ in api.calls)
    assert runtime.in_flight == 0
    assert (tmp_path/'events.jsonl').is_file()
    assert (tmp_path/'record.json').is_file()


@pytest.mark.parametrize('origin', [
    'http://example.com', 'http://localhost:15000', 'http://127.0.0.1:0',
    'http://secret@127.0.0.1:15000', 'http://127.0.0.1:15000/private',
    'http://127.0.0.1:15000?token=secret', 'http://127.0.0.1:15000#secret',
    'ftp://127.0.0.1:15000',
])
def test_native_pilot_only_accepts_explicit_loopback_origin(origin):
    from evaluations.programmatic_pilot.native_api import JsonAPI
    with pytest.raises(ValueError):
        JsonAPI(origin)


def test_native_pilot_provider_mismatch_settles_accepted_task_before_rejecting():
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot.runner import config
    from evaluations.programmatic_pilot.workloads import tool_schemas
    api = _NativePilotAPI(provider='unexpected-provider')
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'}, 'provider-a')
    with pytest.raises(ValueError, match='Provider changed'):
        runtime.start([], config=config('programmatic','medium'),
                      custom_tools=tool_schemas(), custom_tools_mode='exclusive',
                      trajectory='tofu-native', request_id='owned-request', timeout_s=10)
    assert runtime.in_flight == 0
    assert any(path.endswith('/abort') for path,_ in api.calls)


def test_pilot_source_freeze_includes_javascript_engine_and_dependency_artifacts(tmp_path):
    import subprocess
    from evaluations.programmatic_pilot.runner import source_fingerprint
    subprocess.run(['git','init','-q',str(tmp_path)], check=True)
    files = ['lib/tools/javascript/bootstrap.js', 'lib/tools/javascript/assets/quickjs.wasm',
             'bootstrap_pkg/resource_probe.py', 'pyproject.toml', 'uv.lock']
    for relative in files:
        path = tmp_path/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'original bytes')
    subprocess.run(['git','add','.'],cwd=tmp_path,check=True)
    previous = source_fingerprint(tmp_path)
    for relative in files:
        (tmp_path/relative).write_bytes(b'changed bytes')
        current = source_fingerprint(tmp_path)
        assert current != previous
        previous = current


def test_native_pilot_lost_admission_reply_never_retries_and_recovers_only_owned_task():
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot.runner import config
    from evaluations.programmatic_pilot.workloads import tool_schemas

    class LostReply(_NativePilotAPI):
        def call(self, path, body=None):
            if path == '/api/v4/agent/run':
                self.calls.append((path,body))
                raise TimeoutError('accepted request lost its reply')
            if '/by-conv/' in path:
                self.calls.append((path,body))
                return {'tasks':[{'id':'native-task'}]}
            return super().call(path,body)

    api = LostReply()
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    with pytest.raises(TimeoutError):
        runtime.start([], config=config('direct','medium'), custom_tools=tool_schemas(),
                      custom_tools_mode='exclusive', trajectory='tofu-native',
                      request_id='owned-admission', timeout_s=10)
    assert sum(path == '/api/v4/agent/run' for path,_ in api.calls) == 1
    correlation = api.calls[0][1]['conversation_id']
    assert any('/by-conv/' + correlation + '?' in path for path,_ in api.calls)
    assert any(path == '/api/v4/tasks/native-task/abort' for path,_ in api.calls)
    assert runtime.in_flight == 0


@pytest.mark.parametrize('program', [True, False])
def test_native_pilot_capability_gate_rejects_legacy_or_missing_program_evidence(tmp_path, program):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime, capability_probe

    class ProbeAPI(_NativePilotAPI):
        def call(self, path, body=None):
            result = super().call(path,body)
            if path == '/api/v4/tasks/native-task':
                result['content'] = '{"id":"9223372036854775807","runtimeType":"bigint"}'
            return result

    api = ProbeAPI(program=program)
    api.pages[0]['events'][1]['arguments'] = {'dataset':'probe'}
    if program:
        for event in api.pages[1]['events']:
            event['backend'] = 'local_toolscript'
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    with pytest.raises(RuntimeError, match='capability gate failed'):
        asyncio.run(capability_probe(runtime,tmp_path,'medium',10))
    gate = json.loads((tmp_path/'gate.json').read_text())
    assert gate['passed'] is False
    assert gate['countsAsAdoption'] is False
    assert runtime.in_flight == 0
    assert (tmp_path/'record.json').is_file()


def test_native_pilot_http_transport_refuses_redirect_before_forwarding_credentials():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from urllib.error import HTTPError
    from evaluations.programmatic_pilot.native_api import JsonAPI
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header('Location', '/credential-target')
            self.end_headers()
        def log_message(self,*args):
            return

    with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            api = JsonAPI(f'http://127.0.0.1:{server.server_port}',token='synthetic-test-token')
            with pytest.raises(HTTPError):
                api.call('/api/v4/users/me')
            assert seen == ['/api/v4/users/me']
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize('fault', ['call', 'task', 'resolved'])
def test_native_pilot_callback_requires_matching_settlement_receipt(tmp_path, fault):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot.runner import run_task

    class WrongReceipt(_NativePilotAPI):
        def call(self, path, body=None):
            result = super().call(path, body)
            if path.endswith('/tool_result'):
                if fault == 'call':
                    result['callId'] = 'foreign-call'
                elif fault == 'task':
                    result['taskId'] = 'foreign-task'
                else:
                    result['resolved'] = False
            return result

    api = WrongReceipt()
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    record = asyncio.run(run_task(runtime, workloads()[-1], 'programmatic', 0,
                                 tmp_path, 'medium', 10, None))
    assert record['runnerFailure'] == 'RuntimeError'
    assert record['oraclePassed'] is False
    assert runtime.in_flight == 0
    assert any(path.endswith('/abort') for path, _ in api.calls)


def test_native_pilot_foreign_terminal_state_cannot_settle_owned_task(tmp_path):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot.runner import run_task

    class ForeignState(_NativePilotAPI):
        def call(self, path, body=None):
            result = super().call(path, body)
            if path == '/api/v4/tasks/native-task':
                result['id'] = 'foreign-task'
            return result

    runtime = HTTPRuntime(ForeignState(), {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    record = asyncio.run(run_task(runtime, workloads()[-1], 'programmatic', 0,
                                 tmp_path, 'medium', 10, None))
    assert record['runnerFailure'] == 'ValueError'
    assert record['oraclePassed'] is False
    assert runtime.in_flight == 1
    with pytest.raises(RuntimeError, match='has not settled'):
        runtime.close()
    assert runtime.executions[0].cleanup_failure == 'ValueError'


def test_native_pilot_ambiguous_admission_stops_later_dispatch():
    from evaluations.programmatic_pilot.native_api import HTTPRuntime
    from evaluations.programmatic_pilot.runner import config
    from evaluations.programmatic_pilot.workloads import tool_schemas

    class UnresolvedReply(_NativePilotAPI):
        def call(self, path, body=None):
            self.calls.append((path, body))
            if path == '/api/v4/agent/run':
                raise TimeoutError('admission receipt unavailable')
            if '/by-conv/' in path:
                return {'tasks': []}
            raise AssertionError(path)

    api = UnresolvedReply()
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    kwargs = dict(config=config('direct','medium'),custom_tools=tool_schemas(),
                  custom_tools_mode='exclusive',trajectory='tofu-native',
                  request_id='owned-admission',timeout_s=10)
    with pytest.raises(TimeoutError):
        runtime.start([], **kwargs)
    with pytest.raises(RuntimeError, match='has not settled'):
        runtime.start([], **kwargs)
    assert sum(path == '/api/v4/agent/run' for path, _ in api.calls) == 1
    assert runtime.in_flight == 1
    assert not any(path.endswith('/abort') for path, _ in api.calls)


@pytest.mark.parametrize('fault', [None, 'wrong_id', 'wrong_type', 'missing_result', 'error_status', 'unfinished'])
def test_native_probe_uses_completed_program_result_and_keeps_answer_format_scoring(tmp_path, fault):
    import asyncio
    from evaluations.programmatic_pilot.native_api import HTTPRuntime, capability_probe

    class FencedProbe(_NativePilotAPI):
        def call(self, path, body=None):
            result = super().call(path, body)
            if path == '/api/v4/tasks/native-task':
                result['content'] = '```json\n{"id":"9223372036854775807","runtimeType":"bigint"}\n```'
            return result

    api = FencedProbe()
    api.pages[0]['events'][1]['arguments'] = {'dataset':'probe'}
    output = api.pages[1]['events'][-1]
    output['result'] = {'id':'9223372036854775807','runtimeType':'bigint'}
    output['stats']['outcome'] = 'completed'
    if fault == 'wrong_id':
        output['result']['id'] = '9223372036854775808'
    elif fault == 'wrong_type':
        output['result']['runtimeType'] = 'number'
    elif fault == 'missing_result':
        del output['result']
    elif fault == 'error_status':
        output['status'] = 'error'
    elif fault == 'unfinished':
        output['stats']['outcome'] = 'running'
    runtime = HTTPRuntime(api, {'creator_id':'deepseek','model_id':'deepseek-v4-flash'})
    if fault is None:
        assert asyncio.run(capability_probe(runtime,tmp_path,'medium',10)) == 'provider-a'
    else:
        with pytest.raises(RuntimeError, match='capability gate failed'):
            asyncio.run(capability_probe(runtime,tmp_path,'medium',10))
    gate = json.loads((tmp_path/'gate.json').read_text())
    assert gate['passed'] is (fault is None)
    assert gate['modelAnswerOraclePassed'] is False
    assert gate['exactResultAuthority'] == 'native_program_output'
    assert json.loads((tmp_path/'record.json').read_text())['oraclePassed'] is False
    answer = json.loads((tmp_path/'answer.json').read_text())
    assert answer['content'].startswith('```json\n')
    assert answer['status'] == 'done'
    assert runtime.in_flight == 0
