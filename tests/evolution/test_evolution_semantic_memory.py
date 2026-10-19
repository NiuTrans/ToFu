"""Semantic archive recall keeps exact sources, ownership, cost and coverage."""
import json
from copy import deepcopy

import pytest

from lib.evolution import semantic_memory as memory
from lib.evolution.exploration_tools import execute
from tests.evolution.test_evolution_programs import runtime as runtime, create, launch, finish, OWNER
from tests.evolution.test_evolution_exploration_tools import bound_task, call
from tests.goals.test_goal_invocation_integration import runtime as _invocation_runtime_fixture
invocation_runtime = _invocation_runtime_fixture



@pytest.fixture
def archived(runtime, monkeypatch):
    program = launch(runtime, create(runtime))
    program = finish(runtime, program, result='The network saturated; more processors did not improve transfer throughput.')
    runtime[0].command_program(program['program_id'], {'action': 'advance'}, user_id=OWNER)
    program = runtime[0].command_program(program['program_id'], {'action': 'advance'}, user_id=OWNER)
    task = bound_task(runtime, program)
    attempts = []
    def rank(parent, query, chunks):
        from lib.agent_core.execution_budget import model_budget_admission
        attempts.append(query)
        lease = model_budget_admission(parent)(input_tokens=20, max_output_tokens=20)
        lease.mark_dispatched();lease.settle({'prompt_tokens': 20, 'completion_tokens': 11})
        quote = 'network saturated'
        sources = [chunk for chunk in chunks if quote in chunk['text']]
        matches = [{'candidate_id': sources[0]['candidate_id'], 'relevance': .9,
                    'quote': quote, 'reason': 'Related failure by meaning.'}] if sources else []
        return {'matches': matches, 'actuals': {'tokens': 31, 'tool_calls': 0, 'wall_clock_seconds': 1},
                'actuals_complete': True, 'task_id': 'ranking-task',
                'inspector_task_id': 'executor#agent:ranking-task', 'model': 'fixture', 'received_events': 2}
    monkeypatch.setattr(memory, '_rank', rank)
    return program, task, attempts


@pytest.mark.unit
def test_semantic_recall_can_return_a_paraphrased_negative_source_without_claiming_verification(runtime, archived):
    program, task, attempts = archived
    assert call(task, program, 'recall', query='带宽瓶颈')['experiments'] == []
    result = call(task, program, 'semantic_recall', query='带宽瓶颈')
    assert len(result['experiments']) == 1
    source = result['experiments'][0]
    assert source['sequence'] == 1 and source['excerpt'] == 'network saturated'
    assert source['detail_required'] and 'unverified' in result['evidence_status']
    assert attempts == ['带宽瓶颈'] and result['ranking']['actuals']['tokens'] == 31
    assert not result['cache_hit'] and result['cache_persisted']
    assert result['next_cursor'] is None


@pytest.mark.unit
def test_cached_relevance_survives_a_new_task_carrier_without_spending_again(runtime, archived):
    program, task, attempts = archived
    first = call(task, program, 'semantic_recall', query='传输瓶颈')
    replacement = {key: value for key, value in task.items() if not key.startswith('_execution')}
    second = call(replacement, program, 'semantic_recall', query='传输瓶颈')
    assert second['cache_hit'] and second['experiments'] == first['experiments']
    assert len(attempts) == 1 and '_executionBudget' not in replacement
    assert task['_executionBudget'].consumption()[0] == 31
    history = memory.history(program['program_id'], user_id=OWNER)
    assert history['entries'][0]['query'] == '传输瓶颈'
    assert history['entries'][0]['inspector_task_id'] == 'executor#agent:ranking-task'


@pytest.mark.unit
def test_model_selection_and_source_digest_bind_the_cache(runtime, archived):
    program, task, attempts = archived
    call(task, program, 'semantic_recall', query='限速')
    task['model'] = 'new-selected-model'
    changed = call(task, program, 'semantic_recall', query='限速')
    assert not changed['cache_hit'] and len(attempts) == 2
    source = runtime[0].experiment_page(program['program_id'], user_id=OWNER, limit=1)['experiments'][0]
    source['result'] += ' Inspected additional source bytes.'
    key = runtime[0]._experiment_key(program['program_id'], 1, OWNER)
    runtime[0].records.put_record(runtime[0].EXPERIMENTS, key, source, kind='experiment')
    changed_source = call(task, program, 'semantic_recall', query='限速')
    assert not changed_source['cache_hit'] and len(attempts) == 3


@pytest.mark.unit
def test_paused_program_reuses_evidence_but_cannot_spend_on_a_new_semantic_query(runtime, archived):
    program, task, attempts = archived
    call(task, program, 'semantic_recall', query='带宽')
    runtime[0].command_program(program['program_id'], {'action': 'pause'}, user_id=OWNER)
    assert call(task, program, 'semantic_recall', query='带宽')['cache_hit']
    with pytest.raises(memory.EvolutionStateError, match='paused'):
        call(task, program, 'semantic_recall', query='new query')
    assert len(attempts) == 1


@pytest.mark.unit
def test_rank_cache_has_a_bounded_record_and_does_not_erase_source_history(runtime, archived):
    program, task, attempts = archived
    for index in range(36):
        call(task, program, 'semantic_recall', query=f'带宽 {index}')
    history = memory.history(program['program_id'], user_id=OWNER)
    assert len(history['entries']) == history['retained_limit'] == 32
    assert history['entries'][0]['query'] == '带宽 35'
    assert len(memory.receipt_text(history).encode()) < memory.CACHE_BYTES
    assert runtime[0].get_program(program['program_id'], user_id=OWNER)['settled_through'] == 1
    assert runtime[0].experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]['result']
    assert len(attempts) == 36 and task['_executionBudget'].consumption()[0] == 36 * 31


@pytest.mark.unit
def test_every_character_of_a_long_receipt_is_available_to_semantic_windows(runtime, archived):
    program, task, _ = archived
    source = runtime[0].experiment_page(program['program_id'], user_id=OWNER, limit=1)['experiments'][0]
    source['result'] = 'A long archived negative observation. ' * 620
    source['step']['checks'] = ['Inspected evidence: ' + 'constraint ' * 48 for _ in range(16)]
    memory.validate_program(source, 'receipt')
    runtime[0].records.put_record(runtime[0].EXPERIMENTS,
        runtime[0]._experiment_key(program['program_id'], 1, OWNER), source, kind='experiment')
    args = {};pieces = [];seen = set()
    while True:
        chunks, cursor, through = memory.window(program, args, OWNER)
        assert sum(len(chunk['text']) for chunk in chunks) <= memory.WINDOW_CHARS
        pieces.extend(chunk['text'] for chunk in chunks)
        for chunk in chunks:
            assert (chunk['sequence'], chunk['offset']) not in seen
            seen.add((chunk['sequence'], chunk['offset']))
        if cursor is None:
            assert through == 1
            break
        assert cursor['offset'] % memory.CHUNK_CHARS == 0
        args = cursor
    encoded = memory.receipt_text(source)
    assert ''.join(pieces) == encoded
    read = call(task, program, 'read', sequence=1, offset=6000)
    assert read['content'] == encoded[6000:12000]
    assert read['source_digest'] == memory._digest(source)


@pytest.mark.parametrize('arguments', [{'query': ''}, {'query': '   '}, {'query': 'x', 'offset': 1}, {'query': 'x', 'after': 8}])
@pytest.mark.unit
def test_invalid_semantic_input_refuses_before_any_model_work(runtime, archived, arguments):
    program, task, attempts = archived
    with pytest.raises(memory.EvolutionContractError):
        call(task, program, 'semantic_recall', **arguments)
    assert not attempts


@pytest.mark.parametrize('match', [
    {'candidate_id': '9', 'relevance': .9, 'quote': 'network saturated', 'reason': ''},
    {'candidate_id': '0', 'relevance': .9, 'quote': 'invented evidence', 'reason': ''},
    {'candidate_id': '0', 'relevance': True, 'quote': 'network saturated', 'reason': ''},
    {'candidate_id': '0', 'relevance': float('nan'), 'quote': 'network saturated', 'reason': ''},
])
@pytest.mark.unit
def test_model_cannot_create_a_source_quote_or_nonfinite_relevance(runtime, archived, match):
    program, _, _ = archived
    chunks, _, _ = memory.window(program, {}, OWNER)
    with pytest.raises(memory.EvolutionContractError):
        memory._parse_rank(json.dumps({'matches': [match]}), chunks)


@pytest.mark.unit
def test_other_owner_cannot_read_semantic_cache(runtime, archived):
    program, task, _ = archived
    call(task, program, 'semantic_recall', query='带宽')
    with pytest.raises(memory.EvolutionStateError):
        memory.history(program['program_id'], user_id=OWNER + 1)
    task['_userId'] = OWNER + 1
    with pytest.raises(memory.EvolutionStateError):
        call(task, program, 'semantic_recall', query='带宽')


@pytest.mark.api
def test_semantic_rank_uses_common_child_http_pipeline_and_the_parent_budget(invocation_runtime, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler
    from tests.support.loopback import LoopbackThreadingHTTPServer
    from lib.llm.stream import _stream_chat_once
    from lib.tasks_pkg.manager import _stream
    parent = invocation_runtime.parent
    parent['config']['executionLimits'] = {'max_tokens': 100, 'max_tool_calls': 0, 'deadline': None}
    receipts = [{'sequence': 1, 'step': {'title': 'Measured failure'}, 'outcome': 'failed',
                 'result': 'network saturated', 'run_id': 'fixture-run'}]
    text = memory.receipt_text(receipts[0])
    chunks = [{'candidate_id': '0', 'sequence': 1, 'offset': 0, 'end': len(text),
               'source_digest': memory._digest(receipts[0]), 'text': text,
               'title': 'Measured failure', 'outcome': 'failed', 'run_id': 'fixture-run'}]
    bodies = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            bodies.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            ranking = {'matches': [{'candidate_id': '0', 'relevance': .9,
                                   'evidence_id': 0, 'reason': 'Transfer congestion.'}]}
            frame = {'choices': [{'delta': {'content': json.dumps(ranking)}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 20, 'completion_tokens': 10}}
            payload = b'data: ' + json.dumps(frame).encode() + b'\n\ndata: [DONE]\n\n'
            self.send_response(200);self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)));self.end_headers();self.wfile.write(payload)
    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
    import lib.token_counter
    monkeypatch.setattr(lib.token_counter, 'count_tokens', lambda *a, **k: {'tokens': 20})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit', lambda *a, **k: 100000)
    # The provider seam routes to a controlled HTTP peer; real ingress,
    # settlement, child lifecycle and event/usage delivery still execute.
    def dispatch(body, **kwargs):
        body['max_tokens'] = 2048
        return _stream_chat_once(body, base_url=f'http://127.0.0.1:{server.server_port}/v1',
            owner_user_id=kwargs['owner_user_id'], on_content=kwargs.get('on_content'),
            on_thinking=kwargs.get('on_thinking'), abort_check=kwargs.get('abort_check'))
    monkeypatch.setattr(_stream, 'dispatch_stream', dispatch)
    try:
        ranked = memory._rank(parent, '带宽瓶颈', chunks)
        assert len(bodies) == 1 and not bodies[0].get('tools')
        assert bodies[0]['max_tokens'] == 80
        assert ranked['actuals']['tokens'] == 30 and ranked['actuals_complete']
        assert parent['_executionBudget'].consumption() == (30, 0)
        assert not parent['_executionBudget']._model_leases
        assert ranked['received_events'] > 0 and len(invocation_runtime.children) == 1
        child = invocation_runtime.children[0]
        assert child['_parentTaskId'] == parent['id'] and child['_userId'] == 771
        assert not child['_invocationTools'] and child['config']['toolSpecAllowlist'] == []
    finally:
        from lib.llm._transport import reset_pools_for_test
        reset_pools_for_test();server.shutdown();server.server_close();worker.join(timeout=2)


@pytest.mark.unit
def test_cancelled_parent_creates_no_semantic_child(invocation_runtime):
    from lib.llm.llm_errors import AbortedError
    parent = invocation_runtime.parent
    parent['aborted'] = True
    with pytest.raises(AbortedError, match='cancelled'):
        memory._rank(parent, 'Inspect evidence', [])
    assert not invocation_runtime.children


@pytest.mark.unit
def test_unknown_ranking_usage_survives_cache_and_fences_new_finite_requests(runtime, archived, monkeypatch):
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.llm.llm_errors import AbortedError
    program, task, _ = archived
    task['config']['executionLimits'] = {'max_tokens': 100, 'max_tool_calls': None, 'deadline': None}
    def rank(parent, query, chunks):
        lease = model_budget_admission(parent)(input_tokens=20, max_output_tokens=20)
        lease.mark_dispatched();lease.settle(None)
        return {'matches': [], 'actuals': {'tokens': 0, 'tool_calls': 0, 'wall_clock_seconds': 1},
                'actuals_complete': False, 'task_id': 'unknown-usage',
                'inspector_task_id': 'unknown-usage', 'model': 'fixture', 'received_events': 2}
    monkeypatch.setattr(memory, '_rank', rank)
    first = call(task, program, 'semantic_recall', query='带宽')
    assert first['ranking']['actuals_complete'] is False
    assert call(task, program, 'semantic_recall', query='带宽')['cache_hit']
    assert memory.history(program['program_id'], user_id=OWNER)['entries'][0]['actuals_complete'] is False
    with pytest.raises(AbortedError, match='execution_token_usage_unknown'):
        call(task, program, 'semantic_recall', query='another query')
    assert not task['_executionBudget']._model_leases


@pytest.mark.unit
def test_cache_commit_refusal_preserves_delivered_evidence_and_consumption(runtime, archived, monkeypatch):
    from lib.storage.errors import StorageError
    program, task, attempts = archived
    original_put = memory.records.put_record
    def unavailable(namespace, *args, **kwargs):
        if namespace == memory.CACHE_NAMESPACE:
            raise StorageError('database_unavailable', 'Controlled cache failure')
        return original_put(namespace, *args, **kwargs)
    monkeypatch.setattr(memory.records, 'put_record', unavailable)
    result = call(task, program, 'semantic_recall', query='带宽')
    assert result['experiments'][0]['excerpt'] == 'network saturated'
    assert not result['cache_persisted'] and result['cache_error'] == 'database_unavailable'
    assert len(attempts) == 1 and task['_executionBudget'].consumption() == (31, 0)
    assert memory.history(program['program_id'], user_id=OWNER)['entries'] == []
    assert runtime[0].experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]['result']


@pytest.mark.unit
def test_empty_archive_does_not_create_a_model_request(runtime, monkeypatch):
    program = launch(runtime, create(runtime))
    task = bound_task(runtime, program)
    def unexpected(*args):
        pytest.fail('An empty archive cannot need a relevance judge')
    monkeypatch.setattr(memory, '_rank', unexpected)
    result = call(task, program, 'semantic_recall', query='带宽')
    assert result['experiments'] == [] and result['ranking'] is None
    assert result['source_chars'] == 0 and '_executionBudget' not in task


@pytest.fixture
def original_sources(runtime, archived):
    from lib.evolution.stage_outcomes import capture
    from lib.evolution.source_tools import reference
    program, task, attempts = archived
    receipt = runtime[0].experiment_page(program['program_id'], user_id=OWNER, limit=1)['experiments'][0]
    run = runtime[0].runs.get_run(receipt['run_id'], user_id=OWNER)
    run.update(task_id='sealed-original', episode_id='sealed-episode', attempt=1, receipts=[])
    runtime[0].records.put_record(runtime[0].records.NAMESPACE_RUNS,
        runtime[0].records.entity_key(run['run_id'], OWNER, kind='run'), run, kind='run')
    content = '🧪前段观察：没有足够证据。\n' * 2800 + 'TAIL: network saturated'
    tool_content = '工具前段观察。' * 4800 + 'TOOL_TAIL: network saturated'
    terminal = {'id':run['task_id'], '_userId':OWNER, 'status':'done', 'finishReason':'stop',
        'content':content, 'created_at':1, 'finished_at':2,
        'usage':{'input_tokens':10,'output_tokens':5},
        'config':{'evolution':{'run_id':run['run_id'],'stage':'executing'}},
        'messages':[{'role':'tool','tool_call_id':'observation','content':tool_content}]}
    metadata = capture(run, terminal, user_id=OWNER, review_prompt_factory=lambda text:'Frozen review')
    receipt.update(result=content[:24000],result_source=reference(metadata),
        tool_evidence_source=reference(metadata,'tool_evidence'))
    runtime[0].records.put_record(runtime[0].EXPERIMENTS,
        runtime[0]._experiment_key(program['program_id'],1,OWNER),receipt,kind='experiment')
    return program, task, attempts, receipt, content, metadata


@pytest.mark.unit
@pytest.mark.parametrize('kind', ['deliverable','tool_evidence'])
def test_semantic_original_windows_cover_every_unicode_character_and_find_tail_failures(runtime, original_sources, kind):
    from lib.evolution import stage_outcomes
    program, task, attempts, receipt, content, metadata = original_sources
    args={'source_kind':kind};pieces=[];seen=set();last=None
    while True:
        result = call(task,program,'semantic_recall',query='Tail failure',**args)
        chunks,cursor,through=memory.window(program,args,OWNER)
        assert result['source_kind']==kind and result['source_chars']<=memory.WINDOW_CHARS
        assert result['scanned_through']==through and result['next_cursor']==cursor
        for chunk in chunks:
            assert (chunk['sequence'],chunk['offset']) not in seen
            seen.add((chunk['sequence'],chunk['offset']));pieces.append(chunk['text'])
        last=result
        if cursor is None:break
        args={**cursor,'source_kind':kind}
    text=''.join(pieces)
    if kind=='deliverable':assert text==content
    else:
        pages=[];offset=0
        while True:
            page=stage_outcomes.page(receipt['run_id'],metadata['task_id'],user_id=OWNER,offset=offset,source_kind=kind)
            pages.append(page['content'])
            if page['next_offset'] is None:break
            offset=page['next_offset']
        assert text==''.join(pages) and 'TOOL_TAIL: network saturated' in text
    assert last['experiments'][0]['excerpt']=='network saturated'
    source=last['experiments'][0]
    assert source['source_ref']==receipt['result_source' if kind=='deliverable' else 'tool_evidence_source']
    assert source['source_kind']==kind and source['offset']>=24000
    history=memory.history(program['program_id'],user_id=OWNER)
    assert history['entries'][0]['source_chunks'][0]['source_kind']==kind
    assert history['entries'][0]['source_chunks'][0]['run_id']==receipt['run_id']
    cached=call(task,program,'semantic_recall',query='Tail failure',**args)
    assert cached['cache_hit'] and cached['experiments']==last['experiments']
    assert len(attempts)==len(history['entries'])


@pytest.mark.unit
@pytest.mark.parametrize('mutation', ['digest','length','task','part'])
def test_changed_or_unavailable_original_refuses_before_ranking(runtime,original_sources,mutation):
    from lib.evolution import stage_outcomes
    program,task,attempts,receipt,content,metadata=original_sources
    if mutation=='part':
        key=stage_outcomes._part_key(metadata['task_id'],0,OWNER)
        original=runtime[0].records.get_record(stage_outcomes.PART_NAMESPACE,key)
        original['text']='corrupt'+original['text'][7:]
        runtime[0].records.put_record(stage_outcomes.PART_NAMESPACE,key,original,kind='part')
    else:
        reference=receipt['result_source']
        if mutation=='digest':reference['source_sha256']='0'*64
        if mutation=='length':reference['total_chars']+=1
        if mutation=='task':reference['task_id']='foreign-original'
        runtime[0].records.put_record(runtime[0].EXPERIMENTS,
            runtime[0]._experiment_key(program['program_id'],1,OWNER),receipt,kind='experiment')
    with pytest.raises(memory.EvolutionStateError):
        call(task,program,'semantic_recall',query='Tail failure',source_kind='deliverable')
    assert attempts==[]


@pytest.mark.unit
def test_original_scope_never_reports_receipt_preview_as_original_and_keeps_empty_coverage_explicit(runtime,archived):
    program,task,attempts=archived
    result=call(task,program,'semantic_recall',query='network',source_kind='deliverable')
    assert result['experiments']==[] and result['ranking'] is None and not attempts
    assert result['source_kind']=='deliverable' and result['sources_unavailable']==1
    assert result['scanned_through']==program['settled_through'] and result['next_cursor'] is None
    assert 'absence' in result['evidence_status'].lower()


@pytest.mark.unit
@pytest.mark.parametrize('args',[{'action':'recall','source_kind':'deliverable'},
    {'action':'semantic_recall','query':'x','source_kind':'wrong'}])
def test_original_scope_cannot_be_silently_ignored(runtime,archived,args):
    program,task,attempts=archived
    with pytest.raises(memory.EvolutionContractError):
        execute(task,{'program_id':program['program_id'],**args},call_id='invalid-source-scope')
    assert not attempts


@pytest.mark.unit
def test_original_cache_is_separate_from_receipts_and_other_source_kinds(runtime,original_sources):
    program,task,attempts,*_=original_sources
    results=[call(task,program,'semantic_recall',query='Same query',source_kind=kind)
             for kind in ['receipt','deliverable','tool_evidence']]
    assert all(not item['cache_hit'] for item in results) and len(attempts)==3
    for kind in ['receipt','deliverable','tool_evidence']:
        assert call(task,program,'semantic_recall',query='Same query',source_kind=kind)['cache_hit']
    assert len(attempts)==3


@pytest.mark.unit
def test_empty_original_window_preserves_cursor_to_later_receipts_without_spend(runtime,archived,monkeypatch):
    program,task,attempts=archived
    program={**program,'settled_through':51}
    original=runtime[0].experiment_page(program['program_id'],user_id=OWNER)['experiments'][0]
    monkeypatch.setattr(runtime[0],'iter_experiments',lambda pid,user_id,after,limit:iter(
        {**original,'sequence':sequence} for sequence in range(after+1,min(51,after+limit)+1)))
    result=memory.recall(task,program,{'query':'Unknown old evidence','source_kind':'deliverable'})
    assert result['next_cursor']=={'after':50,'offset':0} and result['sources_unavailable']==50
    assert result['scanned_through']==50 and result['ranking'] is None and not attempts


@pytest.mark.unit
@pytest.mark.parametrize('kind',['deliverable','tool_evidence'])
def test_original_spending_respects_pause_and_cached_original_cost(runtime,original_sources,kind):
    program,task,attempts,*_=original_sources
    first=call(task,program,'semantic_recall',query='Cached original query',source_kind=kind)
    runtime[0].command_program(program['program_id'],{'action':'pause'},user_id=OWNER)
    cached=call(task,program,'semantic_recall',query='Cached original query',source_kind=kind)
    assert cached['cache_hit'] and cached['ranking']==first['ranking'] and len(attempts)==1
    with pytest.raises(memory.EvolutionStateError,match='paused'):
        call(task,program,'semantic_recall',query='New original query',source_kind=kind)
    assert len(attempts)==1


@pytest.mark.unit
@pytest.mark.parametrize('kind',['deliverable','tool_evidence'])
def test_original_spending_never_converts_unknown_usage_to_free_or_automatic_retries(runtime,original_sources,monkeypatch,kind):
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.llm.llm_errors import AbortedError
    program,task,*_=original_sources
    task['config']['executionLimits']={'max_tokens':100,'max_tool_calls':None,'deadline':None}
    def unknown(parent,query,chunks):
        lease=model_budget_admission(parent)(input_tokens=20,max_output_tokens=20)
        lease.mark_dispatched();lease.settle(None)
        return {'matches':[],'actuals':{'tokens':0,'tool_calls':0,'wall_clock_seconds':1},
            'actuals_complete':False,'task_id':'unknown-original-usage',
            'inspector_task_id':'unknown-original-usage','model':'fixture','received_events':2}
    monkeypatch.setattr(memory,'_rank',unknown)
    first=call(task,program,'semantic_recall',query='Original query',source_kind=kind)
    assert first['ranking']['actuals_complete'] is False
    assert call(task,program,'semantic_recall',query='Original query',source_kind=kind)['cache_hit']
    with pytest.raises(AbortedError,match='execution_token_usage_unknown'):
        call(task,program,'semantic_recall',query='Another original query',source_kind=kind)
    assert not task['_executionBudget']._model_leases


@pytest.mark.unit
def test_semantic_window_reads_authority_once_and_never_prefetches(runtime,archived):
    programs,store,_=runtime;program,task,_=archived
    original=store.query;reads=[]
    def counted(operation,payload):
        if operation=='record.get':reads.append(payload.copy())
        return original(operation,payload)
    store.query=counted
    chunks,cursor,scanned=memory._receipt_window(program,{},OWNER)
    assert chunks and scanned==1 and cursor is None
    assert sum(row['namespace']==programs.PROGRAMS for row in reads)==1
    assert sum(row['namespace']==programs.EXPERIMENTS for row in reads)==1
    reads.clear()
    iterator=programs.iter_experiments(program['program_id'],user_id=OWNER,limit=50)
    assert sum(row['namespace']==programs.PROGRAMS for row in reads)==1
    assert not any(row['namespace']==programs.EXPERIMENTS for row in reads)
    assert next(iterator)['sequence']==1
    iterator.close()


@pytest.mark.unit
def test_long_lifetime_window_visits_fifty_receipts_with_one_header_read(runtime,archived):
    programs,store,_=runtime;program,task,_=archived
    source=programs.experiment_page(program['program_id'],user_id=OWNER,limit=1)['experiments'][0]
    current,version=programs._read(program['program_id'],OWNER)
    current['settled_through']=50
    programs._save(current,version,OWNER)
    for sequence in range(1,51):
        receipt=deepcopy(source);receipt.update(sequence=sequence,result='short')
        store.rows[(programs.EXPERIMENTS,programs._experiment_key(program['program_id'],sequence,OWNER))]=receipt
    original=store.query;reads=[]
    def counted(operation,payload):
        if operation=='record.get':reads.append(payload.copy())
        return original(operation,payload)
    store.query=counted
    chunks,cursor,scanned=memory._receipt_window(current,{},OWNER)
    headers=sum(row['namespace']==programs.PROGRAMS for row in reads)
    receipts=sum(row['namespace']==programs.EXPERIMENTS for row in reads)
    assert headers==1 and receipts==len({chunk['sequence'] for chunk in chunks})+(1 if cursor and cursor['offset']==0 else 0)
    assert receipts<=50
    # Missing originals in another explicit scope still scan the bounded frontier.
    reads.clear()
    for sequence in range(1,51):
        store.rows[(programs.EXPERIMENTS,programs._experiment_key(program['program_id'],sequence,OWNER))].pop('tool_evidence_source',None)
    _,cursor,scanned,unavailable=memory._source_window(current,{'source_kind':'tool_evidence'},OWNER)
    assert scanned==50 and unavailable==50 and cursor is None
    assert sum(row['namespace']==programs.PROGRAMS for row in reads)==1
    assert sum(row['namespace']==programs.EXPERIMENTS for row in reads)==50


@pytest.fixture
def background_sources(runtime,archived):
    import threading
    import uuid
    from lib.evolution import stage_outcomes
    from lib.evolution.source_tools import reference
    from lib.tasks_pkg import background_commands as commands
    from lib.project_mod.run_command import _format_run_output
    program,task,attempts=archived;programs=runtime[0]
    receipt=programs.experiment_page(program['program_id'],user_id=OWNER,limit=1)['experiments'][0]
    run=programs.runs.get_run(receipt['run_id'],user_id=OWNER)
    run.update(task_id='background-original',episode_id='background-episode',attempt=1,receipts=[],require_command_execution=True)
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(run['run_id'],OWNER,kind='run'),run,kind='run')
    handle='bg_'+uuid.uuid4().hex[:12]
    terminal={'id':run['task_id'],'_userId':OWNER,'status':'done','finishReason':'stop',
        'content':'Waiting for the measurement.','created_at':1,'finished_at':2,
        'usage':{'input_tokens':10,'output_tokens':5},
        'config':{'evolution':{'run_id':run['run_id'],'stage':'executing'}},'messages':[],
        'toolRounds':[{'toolName':'run_command','toolCallId':'launch','status':'done',
            'results':[{'exitCode':'background','backgroundCommandId':handle}]}]}
    assert commands.register_handed_off_command(command_id=handle,task=terminal,command='experiment',done=threading.Event())
    try:
        original=stage_outcomes.capture(run,terminal,user_id=OWNER,review_prompt_factory=lambda text:'Frozen review')
        assert original['pending_background_commands']
        commands.finish_handed_off_command(command_id=handle,result=_format_run_output(
            'experiment','前段观测。🧪'*7000+' BACKGROUND_TAIL: network saturated','',0),error=None)
        state,joined=stage_outcomes.join_background_results(original,user_id=OWNER)
        assert state=='complete'
        metadata=stage_outcomes.read_metadata(run['run_id'],run['task_id'],user_id=OWNER)
        receipt.update(result=terminal['content'],background_evidence_source=reference(metadata,'background_evidence'))
        programs.records.put_record(programs.EXPERIMENTS,programs._experiment_key(program['program_id'],1,OWNER),receipt,kind='experiment')
        yield program,task,attempts,receipt,metadata
    finally:
        with commands._lock:commands._registry.pop(handle,None)


@pytest.mark.unit
def test_background_semantic_scope_covers_complete_late_output_and_caches_separately(runtime,background_sources):
    from lib.evolution import stage_outcomes
    program,task,attempts,receipt,metadata=background_sources
    args={'source_kind':'background_evidence'};pieces=[];result=None
    while True:
        result=call(task,program,'semantic_recall',query='Late counterexample',**args)
        chunks,cursor,scanned=memory.window(program,args,OWNER)
        pieces.extend(chunk['text'] for chunk in chunks)
        assert result['source_kind']=='background_evidence' and result['source_chars']<=memory.WINDOW_CHARS
        if cursor is None:break
        args={**cursor,'source_kind':'background_evidence'}
    text=''.join(pieces)
    assert len(text)==receipt['background_evidence_source']['total_chars'] and len(text)>24000
    assert 'BACKGROUND_TAIL: network saturated' in text
    found=result['experiments'][0]
    assert found['excerpt']=='network saturated' and found['offset']>=24000
    assert found['source_ref']==receipt['background_evidence_source']
    assert found['source_kind']=='background_evidence'
    assert call(task,program,'semantic_recall',query='Late counterexample',**args)['cache_hit']
    missing=call(task,program,'semantic_recall',query='Late counterexample',source_kind='tool_evidence')
    assert missing['sources_unavailable']==1 and not missing['experiments']
    assert memory.history(program['program_id'],user_id=OWNER)['entries'][0]['source_kind']=='background_evidence'


@pytest.mark.unit
@pytest.mark.parametrize('mutation',['digest','task','missing_reference'])
def test_background_scope_refuses_changed_sources_before_ranking(runtime,background_sources,mutation):
    program,task,attempts,receipt,metadata=background_sources
    if mutation=='digest':receipt['background_evidence_source']['source_sha256']='f'*64
    elif mutation=='task':receipt['background_evidence_source']['task_id']='foreign-task'
    else:receipt.pop('background_evidence_source')
    runtime[0].records.put_record(runtime[0].EXPERIMENTS,
        runtime[0]._experiment_key(program['program_id'],1,OWNER),receipt,kind='experiment')
    if mutation=='missing_reference':
        result=call(task,program,'semantic_recall',query='Late counterexample',source_kind='background_evidence')
        assert result['sources_unavailable']==1 and result['ranking'] is None
    else:
        with pytest.raises(memory.EvolutionStateError):
            call(task,program,'semantic_recall',query='Late counterexample',source_kind='background_evidence')
    assert not attempts


@pytest.mark.unit
@pytest.mark.parametrize('state',['done','aborted','abort_event'])
def test_revoked_executor_cannot_use_cached_memory_or_submit_proposal(runtime,archived,state):
    import threading
    from tests.evolution.test_evolution_exploration_tools import candidate
    program,task,attempts=archived
    call(task,program,'semantic_recall',query='Cached negative result')
    assert len(attempts)==1
    if state=='abort_event':
        task['abort_event']=threading.Event();task['abort_event'].set()
    else:task['status']=state
    writes=runtime[1].writes
    for action,args in [('semantic_recall',{'query':'Cached negative result'}),('propose',{'step':candidate()})]:
        with pytest.raises(memory.EvolutionStateError,match='no tool authority'):
            call(task,program,action,**args)
    assert runtime[1].writes==writes and len(attempts)==1


@pytest.mark.unit
def test_cached_semantic_result_rechecks_executor_before_delivery(runtime, archived, monkeypatch):
    from lib.evolution import exploration_tools
    program, task, attempts = archived
    call(task, program, 'semantic_recall', query='Tail failure')
    assert len(attempts) == 1
    recall = memory.recall
    def replace_after_cached_read(*args):
        result = recall(*args)
        assert result['cache_hit']
        run = runtime[0].runs.get_run(program['current']['run_id'], user_id=OWNER)
        run['task_id'] = 'replacement-executor'
        runtime[0].records.put_record(runtime[0].records.NAMESPACE_RUNS,
            runtime[0].records.entity_key(run['run_id'], OWNER, kind='run'), run, kind='run')
        return result
    monkeypatch.setattr(memory, 'recall', replace_after_cached_read)
    with pytest.raises(memory.EvolutionStateError, match='stale or foreign'):
        call(task, program, 'semantic_recall', query='Tail failure')
    assert len(attempts) == 1
