"""Large-delivery handoff, immutable source authority and durable read coverage.

Real records/run settlement and shared tool registry; costly model output is
synthetic. External literature refresh is outside this unit boundary.
"""
from copy import deepcopy
import hashlib

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER
from tests.agent_core.test_model_budget_admission import provider as provider

pytestmark = pytest.mark.unit
CONTENT = ('Observed baseline and causal counterexample 🧪e\u0301\n' * 1200) + 'TAIL_FALSIFIER'


@pytest.fixture(autouse=True)
def no_external_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kwargs: [])


def large_review(runtime):
    runs, _, tasks = runtime
    initial = runs.submit_run({'objective': 'Explain the observed causal counterexample.',
                              'max_attempts': 1}, user_id=OWNER)
    executor = tasks[-1]
    executor['messages'] = [{'role': 'tool', 'tool_call_id': 'actual-baseline',
                             'content': 'Command observed the original baseline failure.'}]
    reviewing = finish(runtime, executor, CONTENT)
    return reviewing, executor, tasks[-1]


def read_args(run, **changes):
    return {'action': 'read', 'run_id': run['run_id'],
            'task_id': run['result_source']['task_id'],
            'source_sha256': run['result_source']['source_sha256'], **changes}


def delivered(task, args, call_id=None, model_projection=None):
    from lib.evolution.source_tools import handle
    from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched
    from lib.agent_core.execution_budget import model_budget_admission
    from types import SimpleNamespace
    import json
    call_id = call_id or 'source-' + str(args.get('offset', 0))
    entries = handle(task, None, 'evolution_source', call_id, args, 1, {},
                     task['config'], '', False)
    entry = entries[0]
    assert entry['fetched'], entry
    text = entry['title'] + ': ' + entry['snippet']
    visible = text if model_projection is None else model_projection(text)
    with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(task)}) as scope:
        lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
        mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease, body={'messages': [
            {'role': 'tool', 'tool_call_id': call_id, 'content': visible}]}))
        scope.usage = {'input_tokens': 1, 'output_tokens': 1}
    return json.loads(entry['snippet'])


def read_all(task, run):
    from lib.evolution.source_tools import execute
    texts = []
    offset = 0
    while True:
        result = delivered(task, read_args(run, offset=offset))
        texts.append(result['content'])
        if result['next_offset'] is None:
            return ''.join(texts)
        offset = result['next_offset']


def test_large_source_enters_independent_review_with_bounded_frozen_prompt(runtime):
    from lib.evolution.stage_launch import read_launch
    run, executor, reviewer = large_review(runtime)
    assert run['status'] == 'reviewing' and len(runtime[2]) == 2
    assert run['result'] == CONTENT[:24000]
    assert run['result_source'] == {'task_id': executor['id'], 'total_chars': len(CONTENT),
        'source_sha256': hashlib.sha256(CONTENT.encode()).hexdigest()}
    frozen = read_launch(run['stage_launch'], run_id=run['run_id'], user_id=OWNER)
    assert len(frozen['prompt']) < 24000
    assert 'Command observed the original baseline failure.' in frozen['prompt']
    assert 'submission_preview_only' in frozen['prompt']
    assert 'TAIL_FALSIFIER' not in frozen['prompt']
    assert reviewer['config']['toolSpecAllowlist'] == ['evolution_source', 'evolution_review']
    assert reviewer['config']['executionLimits']['max_tool_calls'] is None
    assert not reviewer['config']['fetchEnabled'] and not reviewer['config']['codeExecEnabled']


def test_complete_unicode_reads_are_store_read_only_and_allow_review(runtime):
    from lib.evolution.source_tools import execute, fully_read
    run, _, reviewer = large_review(runtime)
    writes = runtime[1].writes
    listed = execute(reviewer, {'action': 'list'})
    assert len(listed['sources']) == 1 and listed['next_cursor'] is None
    assert runtime[1].writes == writes
    from copy import deepcopy
    from lib.evolution.stage_inputs import MODEL_ATTEMPTS_NAMESPACE
    originals=deepcopy({key:value for key,value in runtime[1].rows.items() if key[0]!=MODEL_ATTEMPTS_NAMESPACE})
    assert read_all(reviewer, run) == CONTENT
    assert fully_read(reviewer, run['result_source'])
    # The simulated provider consumes each page and journals model usage;
    # original/source authority remains immutable during those requests.
    assert {key:value for key,value in runtime[1].rows.items() if key[0]!=MODEL_ATTEMPTS_NAMESPACE}==originals
    final = finish(runtime, reviewer, review())
    assert final['status'] == 'completed'


def test_preview_and_forged_prose_cannot_claim_full_acceptance(runtime):
    from lib.evolution.source_tools import execute
    run, _, reviewer = large_review(runtime)
    delivered(reviewer, read_args(run))
    final = finish(runtime, reviewer, review())
    assert final['status'] == 'needs_attention' and final['review']['outcome'] == 'failure'
    assert next(row for row in final['review']['criterion_results']
                if row['criterion_id'] == 'objective')['outcome'] == 'unknown'


def test_missing_middle_and_overlapping_reads_do_not_fake_coverage(runtime):
    from lib.evolution.source_tools import execute, fully_read
    run, _, reviewer = large_review(runtime)
    delivered(reviewer, read_args(run, offset=0))
    delivered(reviewer, read_args(run, offset=1))
    delivered(reviewer, read_args(run, offset=len(CONTENT) - 6000))
    assert not fully_read(reviewer, run['result_source'])
    assert len(reviewer['_evolutionSourceCoverage']['ranges']) == 2
    assert read_all(reviewer, run) == CONTENT
    assert fully_read(reviewer, run['result_source'])


@pytest.mark.parametrize('changes', [
    {'source_sha256': '0' * 64}, {'task_id': 'foreign-task'}, {'run_id': 'foreign-run'},
    {'offset': -1}, {'offset': len(CONTENT) + 1}, {'sequence': 0}, {'action': 'repair'},
])
def test_foreign_changed_or_invalid_source_requests_cannot_record_coverage(runtime, changes):
    from lib.evolution.source_tools import execute
    run, _, reviewer = large_review(runtime)
    with pytest.raises((ValueError, RuntimeError)):
        execute(reviewer, read_args(run, **changes))
    assert '_evolutionSourceCoverage' not in reviewer


def test_stale_reviewer_and_ordinary_chat_have_no_source_authority(runtime):
    from lib.evolution.source_tools import execute
    run, _, reviewer = large_review(runtime)
    ordinary = {'id': 'chat', '_userId': OWNER, 'config': {}}
    with pytest.raises(RuntimeError):
        execute(ordinary, read_args(run))
    current, version = runtime[0]._read(run['run_id'], OWNER)
    current['task_id'] = 'successor-review'
    runtime[0]._save(current, version, OWNER)
    with pytest.raises(RuntimeError):
        execute(reviewer, read_args(run))
    assert '_evolutionSourceCoverage' not in reviewer


def test_authority_change_during_source_read_refuses_delivery_and_coverage(runtime, monkeypatch):
    from lib.evolution import source_tools
    run, _, reviewer = large_review(runtime)
    original = source_tools.page
    def racing(*args, **kwargs):
        result = original(*args, **kwargs)
        current, version = runtime[0]._read(run['run_id'], OWNER)
        current['status'] = 'cancelled'
        runtime[0]._save(current, version, OWNER)
        return result
    monkeypatch.setattr(source_tools, 'page', racing)
    with pytest.raises(RuntimeError):
        source_tools.execute(reviewer, read_args(run))
    assert '_evolutionSourceCoverage' not in reviewer


def test_verified_archive_and_coverage_survive_review_process_loss(runtime, monkeypatch):
    from lib.evolution.stage_outcomes import capture
    from lib.evolution.source_tools import execute
    runs, _, tasks = runtime
    run, _, reviewer = large_review(runtime)
    read_all(reviewer, run)
    reviewer.update(status='done', finishReason='stop', content=review(),
                    usage={'input_tokens': 10, 'output_tokens': 5},
                    finished_at=reviewer['created_at'] + 1)
    capture(run, reviewer, user_id=OWNER, review_prompt_factory=lambda _: None)
    reviewer['_evolutionSourceCoverage']['ranges'] = []
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    count = len(tasks)
    final = runs.advance_run(run['run_id'], user_id=OWNER)
    assert final['status'] == 'completed' and len(tasks) == count
    assert final['receipts'][-1]['restored_from_outcome'] is True


def test_corrupted_source_refuses_read_without_changing_previous_coverage(runtime):
    from lib.evolution import source_tools, stage_outcomes
    run, _, reviewer = large_review(runtime)
    delivered(reviewer, read_args(run))
    coverage = deepcopy(reviewer['_evolutionSourceCoverage'])
    store = runtime[1]
    key = next(key for key in store.rows if key[0] == stage_outcomes.PART_NAMESPACE)
    store.rows[key]['text'] = 'forged original'
    with pytest.raises(RuntimeError):
        source_tools.execute(reviewer, read_args(run, offset=6000))
    assert reviewer['_evolutionSourceCoverage'] == coverage


def test_disconnected_coverage_is_bounded_and_adjacent_reads_release_capacity():
    from lib.evolution.source_tools import _coverage, MAX_INTERVALS
    source = {'task_id': 'executor', 'source_sha256': 'a' * 64, 'total_chars': 1000000}
    coverage = None
    for i in range(MAX_INTERVALS):
        coverage = _coverage(coverage, source, i * 10000, i * 10000 + 6000)
    with pytest.raises(RuntimeError):
        _coverage(coverage, source, MAX_INTERVALS * 10000, MAX_INTERVALS * 10000 + 6000)
    compact = _coverage(coverage, source, 0, MAX_INTERVALS * 10000)
    assert len(compact['ranges']) == 1


def test_registry_exposes_readonly_reader_and_submission_to_large_review(runtime):
    from lib.tools.registry import ToolContext, assemble_tool_list, all_specs
    run, _, reviewer = large_review(runtime)
    context = ToolContext(cfg=reviewer['config'], task_id=reviewer['id'],
        project_path='', project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False, desktop_enabled=False)
    tools, _ = assemble_tool_list(context)
    assert {tool['function']['name'] for tool in tools} == {'evolution_source', 'evolution_review'}
    spec = next(spec for spec in all_specs() if spec.key == 'evolution_source')
    assert not spec.write_tools and not spec.cacheable_tools
    assert 'evolution_source' in spec.idempotent_tools


def test_read_before_result_delivery_is_not_review_coverage(runtime):
    from lib.evolution.source_tools import execute, fully_read
    run, _, reviewer = large_review(runtime)
    execute(reviewer, read_args(run))
    assert not fully_read(reviewer, run['result_source'])


def test_compressed_or_late_results_do_not_acknowledge_original_pages(runtime):
    from lib.evolution.source_tools import handle, fully_read
    from lib.tools.registry._spec import prepare_model_input_observers
    run, _, reviewer = large_review(runtime)
    delivered(reviewer, read_args(run), model_projection=lambda text: text[:100])
    assert not reviewer.get('_evolutionSourceCoverage')
    assert '_evolutionSourcePendingReads' not in reviewer
    args = read_args(run)
    entry = handle(reviewer, None, 'evolution_source', 'late', args, 1, {},
                   reviewer['config'], '', False)[0]
    current, version = runtime[0]._read(run['run_id'], OWNER)
    current['status'] = 'cancelled'
    runtime[0]._save(current, version, OWNER)
    confirmations = prepare_model_input_observers(reviewer, {'messages': [
        {'role': 'tool', 'tool_call_id': 'late', 'content': entry['title'] + ': ' + entry['snippet']}]})
    for confirm in confirmations:
        with pytest.raises(RuntimeError):
            confirm()
    assert not fully_read(reviewer, run['result_source'])
    assert '_evolutionSourcePendingReads' not in reviewer


def test_real_pipeline_only_acknowledges_complete_final_projection(runtime, monkeypatch):
    """Actual dispatch/settlement/L0 projection; only model token output is fake."""
    import json
    from lib.evolution.source_tools import fully_read
    from lib.tasks_pkg.tool_dispatch import _pipeline, _heartbeat
    from lib.tools.registry._build import _handle_evolution_teach
    from lib.tools.registry import ToolContext, assemble_tool_list
    run, _, reviewer = large_review(runtime)
    ctx = ToolContext(cfg=reviewer['config'], task_id=reviewer['id'], project_path='',
        project_enabled=False, search_mode='off', search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False, desktop_enabled=False)
    tools, _ = assemble_tool_list(ctx)
    reviewer['_toolContractDocumentsByName'] = ctx.tool_contract_documents_by_name
    def dispatch(task, tc, name, call_id, args, rn, entry, cfg, path, enabled, all_tools=None, **kwargs):
        return _handle_evolution_teach(task, tc, name, call_id, args, rn, entry, cfg, path, enabled)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', dispatch)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', dispatch)
    messages = []
    for offset in range(0, len(CONTENT), 6000):
        args = read_args(run, offset=offset)
        from tests.tools.test_tool_settle_all_lanes import _mk_tc
        call = _mk_tc('pipeline-' + str(offset), 'evolution_source', offset // 6000, args=args)
        _pipeline.execute_tool_pipeline(reviewer, [call], cfg=reviewer['config'],
            project_path=None, project_enabled=False, tool_list=tools, messages=messages,
            all_search_results_text=[], round_num=offset // 6000, model='test-model')
        # A physical model request sees the final projected messages, after
        # context compilation. This port uses synthetic provider usage only.
        from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched
        from lib.agent_core.execution_budget import model_budget_admission
        from types import SimpleNamespace
        with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(reviewer)}) as scope:
            lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
            mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease, body={'messages': messages}))
            scope.usage = {'input_tokens': 1, 'output_tokens': 1}
    assert fully_read(reviewer, run['result_source'])
    assert '_evolutionSourcePendingReads' not in reviewer
    assert len(messages) == (len(CONTENT) + 5999) // 6000
    assert 'TAIL_FALSIFIER' in messages[-1]['content']


@pytest.mark.parametrize('reader_role', ['executing', 'reviewing', 'reviewing_short'])
@pytest.mark.parametrize('evidence_version', [2, 3])
@pytest.mark.parametrize('input_projection', ['complete', 'clipped'])
def test_current_program_executor_can_read_older_full_original_only_in_its_archive(runtime, reader_role, monkeypatch, evidence_version, input_projection):
    import json
    from lib.evolution import programs
    from lib.evolution.source_tools import execute
    monkeypatch.setattr('lib.evolution.review_evidence.VERSION', evidence_version)
    runs, _, tasks = runtime
    first = programs.create_program({'objective': 'Find a genuine handoff problem and invent a mechanism.',
        'steps': [{'kind': 'experiment', 'title': 'Observe baseline', 'instruction': 'Inspect the baseline.', 'checks': ['Inspect evidence.']},
                  {'kind': 'final', 'title': 'Audit value', 'instruction': 'Verify value.', 'checks': ['Compare baseline.']}]}, user_id=OWNER, idempotency_key='source-program')
    pid = first['program_id']
    started = programs.command_program(pid, {'action': 'start'}, user_id=OWNER)
    executor = tasks[-1]
    reviewing = finish(runtime, executor, CONTENT)
    read_all(tasks[-1], reviewing)
    verdict = {'evidence_audit': {'complete': True, 'objections': []},
        'criterion_results': [{'criterion_id': key, 'outcome': 'pass', 'evidence_ref': 'Observed original.'} for key in runs.RUBRIC],
        'requirement_results': [{'requirement_id': key, 'outcome': 'pass', 'evidence_ref': 'Observed original.'} for key in runs._requirements(reviewing)]}
    finish(runtime, tasks[-1], json.dumps(verdict))
    programs.advance_program(pid, user_id=OWNER)
    programs.advance_program(pid, user_id=OWNER)
    current = programs.advance_program(pid, user_id=OWNER)
    receipt = programs.experiment_page(pid, user_id=OWNER)['experiments'][0]
    assert receipt['result_source'] == reviewing['result_source']
    task = tasks[-1]
    if reader_role.startswith('reviewing'):
        finish(runtime, task, CONTENT if reader_role == 'reviewing' else 'Short current submission.')
        task = tasks[-1]
        assert task['config']['toolSpecAllowlist'] == ['evolution_source', 'evolution_review']
        assert task['config']['executionLimits']['max_tool_calls'] is None
        assert not task['config']['codeExecEnabled']
        if reader_role == 'reviewing_short':
            assert 'Do not execute tools.' not in task['messages'][0]['content']
            empty = execute(task, {'action':'list'})
            assert empty['run_id'] == task['config']['evolution']['run_id']
            if evidence_version == 2:
                assert empty['sources'] == [] and 'review_read_progress' not in empty
            else:
                assert len(empty['sources']) == 1
                assert empty['review_read_progress']['all_sources_delivered'] is False
            assert empty['next_cursor'] is None
            assert not task.get('_evolutionSourceCoverage')
            with pytest.raises(RuntimeError, match='no matching frozen original source' if evidence_version == 2 else 'outside its frozen executor source'):
                execute(task, {'action':'read','run_id':empty['run_id'],
                    'task_id':task['id'],'source_sha256':'0'*64})
    listed = execute(task, {'action':'list','program_id':pid,'sequence':1})
    assert listed['run_id'] == reviewing['run_id']
    original = next(source for source in listed['sources'] if source['task_id'] == reviewing['result_source']['task_id'])
    assert original['source_sha256'] == reviewing['result_source']['source_sha256']
    args = {'action':'read','program_id':pid,'sequence':1,
        'run_id':listed['run_id'],'task_id':original['task_id'],
        'source_sha256':original['source_sha256'],'offset':len(CONTENT)-6000}
    projection = (lambda text: text[:80]) if input_projection == 'clipped' else None
    result = delivered(task, args, model_projection=projection) if reader_role.startswith('reviewing') else execute(task, args)
    assert result['content'].endswith('TAIL_FALSIFIER')
    assert not task.get('_evolutionSourceCoverage')
    assert not task.get('_evolutionSourcePendingReads')
    history = task.get('_evolutionHistoricalSourceCoverage') or []
    if evidence_version == 3 and reader_role.startswith('reviewing') and input_projection == 'complete':
        assert len(history) == 1
        assert history[0]['source']['task_id'] == original['task_id']
        assert history[0]['ranges'] == [[len(CONTENT)-6000, len(CONTENT)]]
    else:
        assert not history
    from lib.evolution import source_tools
    read_metadata = source_tools.read_metadata
    original_receipt = deepcopy(receipt)
    def change_membership(*args, **kwargs):
        result = read_metadata(*args, **kwargs)
        changed = deepcopy(original_receipt)
        changed['run_id'] = task['config']['evolution']['run_id']
        programs.records.put_record(programs.EXPERIMENTS,
            programs._experiment_key(pid, 1, OWNER), changed, kind='experiment')
        return result
    try:
        with monkeypatch.context() as patch:
            patch.setattr(source_tools, 'read_metadata', change_membership)
            with pytest.raises(RuntimeError, match='binding changed'):
                execute(task, {'action':'list','program_id':pid,'sequence':1})
    finally:
        programs.records.put_record(programs.EXPERIMENTS,
            programs._experiment_key(pid, 1, OWNER), original_receipt, kind='experiment')
    if reader_role.startswith('reviewing'):
        import json
        current_run = runs.get_run(task['config']['evolution']['run_id'], user_id=OWNER)
        value, version = runs._read(current_run['run_id'], OWNER)
        frozen_context = json.loads(value['context_seed'])
        frozen_context['settled_through'] = 0
        value['context_seed'] = json.dumps(frozen_context)
        runs._save(value, version, OWNER)
        with pytest.raises(RuntimeError, match='frozen program history boundary'):
            execute(task, args)
        with pytest.raises(RuntimeError, match='frozen program history boundary'):
            execute(task, {'action':'list','program_id':pid,'sequence':1})
    with pytest.raises(RuntimeError):
        execute(task, {k: v for k, v in args.items() if k != 'sequence'})
    with pytest.raises(RuntimeError):
        execute(task, {**args, 'program_id': 'foreign-program'})


def test_small_retry_page_preserves_unicode_offsets_and_explicit_tool_budget(runtime):
    from lib.evolution.source_tools import fully_read
    run, _, reviewer = large_review(runtime)
    result = delivered(reviewer, read_args(run, limit=37))
    assert result['content'] == CONTENT[:37] and result['next_offset'] == 37
    assert not fully_read(reviewer, run['result_source'])
    initial = runtime[0].submit_run({'objective': 'Bound source review work.',
        'max_attempts': 1, 'budget': {'max_tool_calls': 1}}, user_id=OWNER)
    executor = runtime[2][-1]
    from lib.agent_core.execution_limits import admit_tool_execution
    from lib.agent_core.execution_budget import model_budget_admission
    model_budget_admission(executor)
    assert admit_tool_execution(executor) is None
    final = finish(runtime, executor, CONTENT)
    assert final['status'] == 'needs_attention'
    assert 'budget_exhausted' in final['error']
    assert final['actuals']['tool_calls'] == 1
    assert runtime[2][-1] is executor


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic', 'gemini'])
def test_final_native_provider_input_confirms_only_exact_original_text(runtime, protocol):
    from types import SimpleNamespace
    from lib.evolution.source_tools import handle
    from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched, seal_request
    from lib.agent_core.execution_budget import model_budget_admission
    run, _, reviewer = large_review(runtime)
    args = read_args(run, limit=137)
    entry = handle(reviewer, None, 'evolution_source', 'native', args, 1, {}, reviewer['config'], '', False)[0]
    text = entry['title'] + ': ' + entry['snippet']
    if protocol == 'openai':
        body = {'messages': [{'role': 'tool', 'tool_call_id': 'native', 'content': text}]}
    elif protocol == 'responses':
        body = {'input': [{'type': 'function_call_output', 'call_id': 'native', 'output': text}]}
    elif protocol == 'anthropic':
        body = {'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'native',
                             'content': [{'type': 'text', 'text': text}]}]}]}
    else:
        body = {'contents': [{'role': 'function', 'parts': [{'functionResponse':
            {'name': 'evolution_source', 'response': {'content': text}}}]}]}
    admitted = seal_request(body, model='test', protocol=protocol, input_tokens=1, semantic_id='synthetic-source-input')
    with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(reviewer)}) as scope:
        lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
        mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease, body=admitted))
        assert not reviewer.get('_evolutionSourceCoverage')
        scope.usage = {'input_tokens': 1, 'output_tokens': 1}
    assert reviewer['_evolutionSourceCoverage']['ranges'] == [[0, 137]]


def test_failed_provider_request_and_unsent_lease_cannot_confirm_source(runtime):
    from types import SimpleNamespace
    from lib.evolution.source_tools import handle
    from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched
    from lib.agent_core.execution_budget import model_budget_admission
    run, _, reviewer = large_review(runtime)
    args = read_args(run, limit=137)
    entry = handle(reviewer, None, 'evolution_source', 'failed', args, 1, {}, reviewer['config'], '', False)[0]
    body = {'messages': [{'role': 'tool', 'tool_call_id': 'failed', 'content': entry['title'] + ': ' + entry['snippet']}]}
    with pytest.raises(ValueError):
        with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(reviewer)}) as scope:
            lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
            mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease, body=body))
            raise ValueError('synthetic provider rejection')
    assert not reviewer.get('_evolutionSourceCoverage')
    assert reviewer['_evolutionSourcePendingReads']
    # Preparing input without physical send also supplies no access proof.
    with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(reviewer)}) as scope:
        lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
        lease.prepare_input(body)
    assert not reviewer.get('_evolutionSourceCoverage')


@pytest.mark.parametrize('final_is_complete', [False, True])
def test_only_final_successful_attempt_confirms_current_source_input(runtime, final_is_complete):
    from types import SimpleNamespace
    from lib.evolution.source_tools import handle
    from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched
    from lib.agent_core.execution_budget import model_budget_admission
    run, _, reviewer = large_review(runtime)
    args = read_args(run, limit=137)
    entry = handle(reviewer, None, 'evolution_source', 'retry', args, 1, {}, reviewer['config'], '', False)[0]
    text = entry['title'] + ': ' + entry['snippet']
    with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(reviewer)}) as scope:
        for value in (text, text if final_is_complete else text[:50]):
            lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
            mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease,
                body={'messages': [{'role': 'tool', 'tool_call_id': 'retry', 'content': value}]}))
        scope.usage = {'input_tokens': 1, 'output_tokens': 1}
    assert bool(reviewer.get('_evolutionSourceCoverage')) is final_is_complete
    assert '_evolutionSourcePendingReads' not in reviewer


@pytest.mark.parametrize('asynchronous', [False, True])
def test_real_http_source_receipt_confirms_sealed_model_input(runtime, provider, asynchronous):
    """Real HTTP + native sync/async ingress; provider response is synthetic."""
    import asyncio
    import json
    from lib.evolution.source_tools import handle
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.llm.stream import stream_chat
    from lib.llm.astream import async_stream_chat
    url, bodies, _options = provider
    run, _, reviewer = large_review(runtime)
    args = read_args(run, limit=137)
    entry = handle(reviewer, None, 'evolution_source', 'http-source', args, 1, {}, reviewer['config'], '', False)[0]
    original = entry['title'] + ': ' + entry['snippet']
    body = {'model': 'gpt-4o', 'max_tokens': 100, 'stream': True,
        'messages': [{'role': 'user', 'content': 'Inspect the original source.'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'http-source', 'type': 'function',
                'function': {'name': 'evolution_source', 'arguments': json.dumps(args)}}]},
            {'role': 'tool', 'tool_call_id': 'http-source', 'content': original}],
        '_model_budget_admit': model_budget_admission(reviewer)}
    if asynchronous:
        asyncio.run(async_stream_chat(body, base_url=url, owner_user_id=OWNER))
    else:
        stream_chat(body, base_url=url, owner_user_id=OWNER)
    assert len(bodies) == 1 and bodies[0]['messages'][-1]['content'] == original
    assert '_model_budget_admit' not in bodies[0]
    assert reviewer['_evolutionSourceCoverage']['ranges'] == [[0, 137]]


def test_exploration_approval_shows_program_and_proposed_mechanism():
    from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
    from tests.evolution.test_evolution_exploration_tools import candidate
    args = {'program_id': 'current-program', 'action': 'propose', 'step': candidate()}
    meta = {}
    _APPROVAL_META_ENRICHERS['evolution_explore'](meta, args)
    encoded = str(meta)
    assert 'current-program' in encoded and candidate()['hypothesis']['mechanism'] in encoded
    assert 'no automatic capability activation' in encoded


def test_nonempty_original_after_whitespace_preview_still_enters_review(runtime):
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Inspect the complete experiment.', 'max_attempts': 1}, user_id=OWNER)
    content = ' ' * 25000 + 'The observed falsifier is at the end.'
    run = finish(runtime, tasks[-1], content)
    assert run['status'] == 'reviewing' and run['result_source']['total_chars'] == len(content)
    assert not run['result'].strip()
    assert read_all(tasks[-1], run) == content
    assert finish(runtime, tasks[-1], review())['status'] == 'completed'


def test_model_input_observer_cannot_leak_into_another_request_plugin_scope(monkeypatch):
    from lib.tools.registry import _spec
    calls = []
    spec = _spec.ToolSpec('input-probe', lambda _ctx: [], source='plugin',
        plugin_name='private-input-probe', provides=frozenset({'probe_input'}),
        model_input_observer=lambda task, body: calls.append((task['_userId'], body)))
    monkeypatch.setattr(_spec, '_TOOL_SPECS', [*_spec._TOOL_SPECS, spec])
    wire = {'messages': [{'role': 'user', 'content': 'synthetic private request'}]}
    first = {'_userId': OWNER, 'config': {}, '_executableToolCatalog': []}
    _spec.prepare_model_input_observers(first, wire)
    assert calls == []
    first['_executableToolCatalog'] = [{'type': 'function', 'function': {'name': 'probe_input'}}]
    _spec.prepare_model_input_observers(first, wire)
    assert len(calls) == 1
    other = {'_userId': OWNER + 1, 'config': {}, '_executableToolCatalog': []}
    _spec.prepare_model_input_observers(other, wire)
    first['config']['toolSpecDenylist'] = ['input-probe']
    _spec.prepare_model_input_observers(first, wire)
    first['config'] = {'toolSpecAllowlist': []}
    _spec.prepare_model_input_observers(first, wire)
    assert len(calls) == 1


@pytest.mark.parametrize('projection, acknowledged', [
    ('plain_text_parts', True), ('cache_marker', True), ('split_text', True),
    ('mixed_image', False), ('missing_text', False), ('truncated_text', False),
])
def test_final_openai_tool_text_parts_preserve_exact_coverage(runtime, provider, monkeypatch, projection, acknowledged):
    """Real HTTP request scope with cache-marked/split OpenAI tool text.

    Provider response is synthetic; the actual successful sealed input confirms
    only a complete text-only projection. Source producer reads alone do not.
    """
    import json
    from lib.evolution.source_tools import handle
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.llm.stream import stream_chat
    url, bodies, _ = provider
    run, _, reviewer = large_review(runtime)
    args = read_args(run, limit=137)
    entry = handle(reviewer, None, 'evolution_source', 'parts', args, 1, {}, reviewer['config'], '', False)[0]
    original = entry['title'] + ': ' + entry['snippet']
    content = [{'type': 'text', 'text': original, 'cache_control': {'type': 'ephemeral'}}]
    if projection == 'plain_text_parts':
        content[0].pop('cache_control')
        monkeypatch.setattr('lib.llm.cache._gateway_honors_cache_markers', lambda model: True)
    elif projection == 'split_text':
        content = [{'type': 'text', 'text': original[:100], 'cache_control': {'type': 'ephemeral'}},
                   {'type': 'text', 'text': original[100:]}]
    elif projection == 'mixed_image':
        content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}})
    elif projection == 'missing_text':
        content.append({'type': 'text'})
    elif projection == 'truncated_text':
        content[0]['text'] = original[:-1]
    body = {'model': 'gpt-4o', 'max_tokens': 100, 'stream': True,
        'messages': [{'role': 'user', 'content': 'Inspect this exact source.'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'parts', 'type': 'function',
                'function': {'name': 'evolution_source', 'arguments': json.dumps(args)}}]},
            {'role': 'tool', 'tool_call_id': 'parts', 'content': content}],
        '_model_budget_admit': model_budget_admission(reviewer)}
    stream_chat(body, base_url=url, owner_user_id=OWNER)
    assert len(bodies) == 1
    coverage = reviewer.get('_evolutionSourceCoverage')
    assert bool(coverage) is acknowledged
    if acknowledged:
        assert coverage['ranges'] == [[0, 137]]


@pytest.mark.parametrize('offset,limit,ordinals',[(29999,1,[0]),(29990,37,[0,1]),(30000,137,[1])])
def test_requested_page_limit_controls_original_part_reads(runtime,monkeypatch,offset,limit,ordinals):
    from lib.evolution import stage_outcomes
    from lib.evolution.source_tools import execute,fully_read
    run,_,reviewer=large_review(runtime);seen=[];read=stage_outcomes._read_part
    def observed(value,ordinal,*args,**kwargs):
        seen.append(ordinal)
        return read(value,ordinal,*args,**kwargs)
    monkeypatch.setattr(stage_outcomes,'_read_part',observed)
    result=execute(reviewer,read_args(run,offset=offset,limit=limit))
    assert result['content']==CONTENT[offset:offset+limit]
    assert result['next_offset']==offset+limit and seen==ordinals
    assert not fully_read(reviewer,run['result_source'])


@pytest.mark.parametrize('limit',[0,-1,6001,True,1.5,None])
def test_invalid_original_page_limit_refuses_before_storage_read(monkeypatch,limit):
    from lib.evolution import stage_outcomes
    def no_read(*args,**kwargs):raise AssertionError('invalid limit reached storage')
    monkeypatch.setattr('lib.evolution.runs.get_run',no_read)
    with pytest.raises(ValueError,match='page limit'):
        stage_outcomes.page('run-invalid','task-invalid',user_id=OWNER,limit=limit)


@pytest.mark.parametrize('change', ['cancel','replace_reviewer','stop'])
def test_delivery_confirmation_rechecks_authority_after_source_validation(runtime, monkeypatch, change):
    import threading
    from lib.evolution import source_tools
    run, _, reviewer = large_review(runtime)
    entry = source_tools.handle(reviewer,None,'evolution_source','race',read_args(run),1,{},
        reviewer['config'],'',False)[0]
    confirmation = source_tools.prepare_model_input(reviewer, {'messages':[
        {'role':'tool','tool_call_id':'race','content':entry['title']+': '+entry['snippet']}]})
    original = source_tools._review_source
    def changed_during_read(*args, **kwargs):
        source = original(*args, **kwargs)
        if change == 'stop':
            reviewer['abort_event'] = threading.Event();reviewer['abort_event'].set()
        else:
            value, version = runtime[0]._read(run['run_id'],OWNER)
            if change == 'cancel': value['status'] = 'cancelled'
            else: value['task_id'] = 'replacement-reviewer'
            runtime[0]._save(value,version,OWNER)
        return source
    monkeypatch.setattr(source_tools,'_review_source',changed_during_read)
    with pytest.raises(RuntimeError): confirmation()
    assert not reviewer.get('_evolutionSourceCoverage')
    assert not reviewer.get('_evolutionHistoricalSourceCoverage')
    assert not reviewer.get('_evolutionSourcePendingReads')


@pytest.mark.parametrize('current,history,expected', [
    (None,None,'submit_independent_review'),
    ({'all_sources_delivered':False},None,'read_current_sources'),
    ({'all_sources_delivered':False},{'sources':[],'all_sources_delivered':False},'read_current_sources'),
    ({'all_sources_delivered':True},{'sources':[{}],'all_sources_delivered':False},'read_historical_sources'),
    ({'all_sources_delivered':True},{'sources':[],'all_sources_delivered':False},'submit_unknown_review'),
    (None,{'sources':[],'all_sources_delivered':False},'submit_unknown_review'),
    ({'all_sources_delivered':True},{'sources':[{}],'all_sources_delivered':True},'submit_independent_review'),
])
def test_combined_review_action_keeps_current_and_history_scopes_distinct(current,history,expected):
    from lib.evolution.source_tools import review_next_action
    assert review_next_action(current,history) == expected


@pytest.mark.parametrize('initial', ['absent','missing','available','legacy'])
def test_history_progress_reads_lineage_once_and_never_caches_later_authority(monkeypatch, initial):
    from lib.evolution import programs, source_tools
    source = {'task_id':'parent-task','source_sha256':'a'*64,'total_chars':100}
    observations = {'absent':{}, 'missing':{'validation_feedback_reuse':{'parent_sources':{}}},
        'available':{'validation_feedback_reuse':{'parent_sources':{'tool_evidence_source':source}}}, 'legacy':{}}
    current = [observations[initial]]; calls = []
    def lookup(run, *, user_id):
        calls.append(user_id)
        return current[0]
    monkeypatch.setattr(programs,'validation_generation_review_input',lookup)
    run = {'evidence_review_version':1 if initial == 'legacy' else 2}
    first = source_tools.history_read_progress({},run,owner=OWNER)
    assert calls == ([] if initial == 'legacy' else [OWNER])
    if initial in ('legacy','absent'): assert first is None
    elif initial == 'missing': assert first['sources'] == [] and not first['all_sources_delivered']
    else: assert first['sources'][0]['source']['task_id'] == 'parent-task'
    if initial != 'legacy':
        current[0] = observations['available']
        later = source_tools.history_read_progress({},run,owner=OWNER+1)
        assert calls == [OWNER,OWNER+1]
        assert later['sources'][0]['source']['task_id'] == 'parent-task'


@pytest.mark.parametrize('history_delivery', ['complete', 'partial', 'clipped', 'future_scope', 'foreign_program', 'wrong_run', 'missing_scope'])
@pytest.mark.parametrize('evidence_version', [2, 3])
def test_v3_historical_requirement_link_needs_scoped_complete_delivery(runtime, history_delivery, evidence_version, monkeypatch):
    import json
    from lib.evolution.contracts import EvolutionContractError, EvolutionStateError
    from lib.evolution import programs, stage_outcomes, review_delivery
    from lib.evolution.source_tools import execute, requirement_originals_match
    monkeypatch.setattr('lib.evolution.review_evidence.VERSION', evidence_version)
    runs, _, tasks = runtime
    program = programs.create_program({'objective':'Compare actual baseline evidence in the final audit.',
        'max_attempts':1, 'max_review_format_retries':0, 'steps':[{'kind':'experiment','title':'Baseline','instruction':'Observe the baseline.','checks':['Inspect baseline.']},
                 {'kind':'final','title':'Audit','instruction':'Audit the original objective.','checks':['Inspect prior evidence.']}]},
        user_id=OWNER,idempotency_key='historical-requirement-native')
    pid = program['program_id']
    programs.command_program(pid,{'action':'start'},user_id=OWNER)
    first = finish(runtime,tasks[-1],CONTENT)
    read_all(tasks[-1],first)
    value = {'evidence_audit':{'complete':True,'objections':[]},
        'criterion_results':[{'criterion_id':key,'outcome':'pass','evidence_ref':'Inspected original.'} for key in runs.RUBRIC],
        'requirement_results':[{'requirement_id':key,'outcome':'pass','evidence_ref':'Inspected original.'} for key in runs._requirements(first)]}
    finish(runtime,tasks[-1],json.dumps(value))
    programs.advance_program(pid,user_id=OWNER)
    programs.advance_program(pid,user_id=OWNER)
    programs.advance_program(pid,user_id=OWNER)
    current = finish(runtime,tasks[-1],CONTENT)
    task = tasks[-1]
    read_all(task,current)
    source = deepcopy(first['result_source'])
    args = {'action':'read','program_id':pid,'sequence':1,'run_id':first['run_id'],
        'task_id':source['task_id'],'source_sha256':source['source_sha256'],'offset':0}
    while True:
        page = delivered(task,args,model_projection=(lambda text:text[:80]) if history_delivery == 'clipped' else None)
        if history_delivery in ('partial', 'clipped') or page['next_offset'] is None: break
        args['offset'] = page['next_offset']
    coverage = task.get('_evolutionHistoricalSourceCoverage') or []
    if coverage and history_delivery == 'future_scope': coverage[0]['historical_target']['sequence'] = 2
    if coverage and history_delivery == 'foreign_program': coverage[0]['historical_target']['program_id'] = 'program-foreign'
    if coverage and history_delivery == 'wrong_run': coverage[0]['historical_target']['run_id'] = current['run_id']
    if coverage and history_delivery == 'missing_scope': coverage[0].pop('historical_target', None)
    accepted = evidence_version == 3 and history_delivery == 'complete'
    assert requirement_originals_match(task,current,[source],checked_sources={},require_delivery=True) == accepted
    value['requirement_results'] = [{'requirement_id':key,'outcome':'pass','evidence_ref':'Inspected owned prior original.',
        'original_sources':[source]} for key in runs._requirements(current)]
    request = {'review':deepcopy(value)}
    message = {'role':'assistant','content':'','tool_calls':[{'id':'review-call','type':'function',
        'function':{'name':review_delivery.NAME,'arguments':json.dumps(request)}}]}
    task['_compact_messages'] = [message]
    if not accepted:
        with pytest.raises((EvolutionContractError, EvolutionStateError)):
            review_delivery.prepare(task,request,call_id='review-call',round_num=0)
        assert runs.get_run(current['run_id'],user_id=OWNER)['status'] == 'reviewing'
        assert len(tasks) == 4 and '_explicitToolDeliveryRequest' not in task
    if accepted:
        from tests.evolution.test_evolution_review_delivery import acknowledge
        prepared = review_delivery.prepare(task,request,call_id='review-call',round_num=0)
        assert acknowledge(task,message,prepared)
        content = task['content']
    else:
        content = json.dumps(value)
    settled = finish(runtime,task,content)
    assert settled['status'] == ('completed' if accepted else 'needs_attention')
    if current.get('require_requirement_originals') and not accepted:
        assert settled['review'] is None
        assert settled['receipts'][-1]['review_format_retry']['error'] == 'originals_uninspected'
    else:
        assert all(row['outcome'] == ('pass' if accepted else 'unknown')
            for row in settled['review']['requirement_results'])
    metadata = stage_outcomes.read_metadata(current['run_id'],task['id'],user_id=OWNER)
    if evidence_version == 3 and history_delivery in ('complete', 'partial'):
        assert metadata['historical_source_coverage'][0]['historical_target'] == {
            'program_id':pid,'sequence':1,'run_id':first['run_id']}
        _outcome, restored = stage_outcomes.recover(current,user_id=OWNER)
        assert restored['_evolutionHistoricalSourceCoverage'] == metadata['historical_source_coverage']
        assert requirement_originals_match(restored,current,[source],checked_sources={},require_delivery=True) == accepted


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic', 'gemini'])
@pytest.mark.parametrize('deliver_second', [True, False])
def test_one_provider_input_confirms_only_delivered_batch_pages(runtime, protocol, deliver_second):
    from types import SimpleNamespace
    from lib.evolution.source_tools import handle
    from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched, seal_request
    from lib.agent_core.execution_budget import model_budget_admission
    run, _, reviewer = large_review(runtime)
    outputs = []
    for index in range(2):
        entry = handle(reviewer, None, 'evolution_source', 'batch-' + str(index),
                       read_args(run, offset=index * 137, limit=137),
                       index + 1, {}, reviewer['config'], '', False)[0]
        outputs.append(entry['title'] + ': ' + entry['snippet'])
    delivered = outputs if deliver_second else outputs[:1]
    if protocol == 'openai':
        body = {'messages': [{'role': 'tool', 'tool_call_id': 'batch-' + str(i),
                              'content': text} for i, text in enumerate(delivered)]}
    elif protocol == 'responses':
        body = {'input': [{'type': 'function_call_output', 'call_id': 'batch-' + str(i),
                           'output': text} for i, text in enumerate(delivered)]}
    elif protocol == 'anthropic':
        body = {'messages': [{'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'batch-' + str(i),
             'content': [{'type': 'text', 'text': text}]}
            for i, text in enumerate(delivered)]}]}
    else:
        body = {'contents': [{'role': 'function', 'parts': [
            {'functionResponse': {'name': 'evolution_source', 'response': {'content': text}}}
            for text in delivered]}]}
    admitted = seal_request(body, model='test', protocol=protocol, input_tokens=1,
                            semantic_id='batched-source-input')
    with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(reviewer)}) as scope:
        lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
        mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease, body=admitted))
        assert not reviewer.get('_evolutionSourceCoverage')
        scope.usage = {'input_tokens': 1, 'output_tokens': 1}
    assert reviewer['_evolutionSourceCoverage']['ranges'] == [[0, 274 if deliver_second else 137]]
