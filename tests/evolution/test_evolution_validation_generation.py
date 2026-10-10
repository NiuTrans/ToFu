"""Reviewed candidate successors preserve frozen controls and immutable failures."""
from copy import deepcopy
import json
import pytest
from lib.evolution import programs
from lib.evolution.contracts import EvolutionStateError, EvolutionContractError
from lib.evolution.exploration_tools import execute
from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER
from tests.evolution.test_evolution_scientific_observations import protocol, command
from tests.support.research_observations import observation_payload, synthetic_frozen_scope
from tests.evolution.test_evolution_exploration_tools import candidate

pytestmark = pytest.mark.unit


@pytest.fixture(params=[False, True], ids=['legacy-observations','identified-observations'])
def identified_observations(request):
    return request.param


@pytest.fixture
def failed_validation(runtime, request, identified_observations):
    runs, store, tasks = runtime
    frozen = protocol();frozen['measurement_scope'] = synthetic_frozen_scope(frozen)
    if identified_observations:
        frozen['observation_format'] = 'tofu.scientific-observations/v2'
    step = {'kind':'experiment','title':'Validate integrated candidate','instruction':'Measure the frozen candidate',
        'checks':['Frozen effect target is met.'], 'requires_scientific_protocol':True,
        'requires_frozen_measurement_scope':True, 'scientific_protocol':frozen, 'scientific_required_outcome':'supported'}
    if identified_observations:
        step['required_observation_format'] = frozen['observation_format']
    program = programs.create_program({'objective':'Improve the system under the original matched comparison',
        'max_attempts':getattr(request,'param',1),'steps':[step,{'kind':'final','title':'Audit','instruction':'Audit original objective','checks':['Original objective satisfied.']}]},
        user_id=OWNER,idempotency_key='generation-parent')
    active = programs.command_program(program['program_id'],{'action':'start'},user_id=OWNER)
    payload = observation_payload(frozen,'metrics','refuted')
    comparison = payload['scientific_observations']['comparison']
    comparison['method']['system_digest'] = frozen['measurement_scope']['candidate_digest']
    comparison['baseline']['system_digest'] = frozen['measurement_scope']['baseline_digest']
    tasks[-1].update(command(payload))
    failed = finish(runtime,tasks[-1],'Observed negative result.')
    assert failed['error'] == 'scientific_outcome_requirement_unmet'
    active = programs.advance_program(program['program_id'],user_id=OWNER)
    assert active['queue'][0]['kind'] == 'replan'
    programs.advance_program(program['program_id'],user_id=OWNER)
    active = programs.advance_program(program['program_id'],user_id=OWNER)
    return active, step, frozen, tasks[-1], failed


def proposal(task, active):
    followup = candidate();followup['kind'] = 'synthesis'
    followup['instruction'] = 'Prepare a changed candidate and freeze its scope, preserving all original controls.'
    return execute(task,{'program_id':active['program_id'],'action':'replan',
        'reason':'Confirmed negative measurements require an improved candidate generation.',
        'steps':[followup], 'refresh_validation_generation':True},call_id='reviewed-generation')


def accept_diagnosis(runtime, active, task):
    proposal(task, active)
    finish(runtime,task,'The candidate changes; original controls and outcome remain fixed.')
    assert 'candidate_generation_refresh' in runtime[2][-1]['messages'][0]['content']
    assert 'refresh_validation_generation' in runtime[2][-1]['messages'][0]['content']
    judgment = json.loads(review())
    current = runtime[0].get_run(active['current']['run_id'],user_id=OWNER)
    judgment['requirement_results'] = [{'requirement_id':f'requirement-{i+1}','outcome':'pass',
        'evidence_ref':'The reviewed proposal preserves controls and archived failure.'}
        for i in range(len(current['acceptance_requirements']))]
    finish(runtime,runtime[2][-1],json.dumps(judgment))
    return programs.advance_program(active['program_id'],user_id=OWNER)


def test_only_review_admits_successor_and_old_failure_remains_immutable(runtime, failed_validation):
    active, original_step, frozen, task, failed = failed_validation
    before = deepcopy(programs.experiment_page(active['program_id'],user_id=OWNER)['experiments'])
    result = proposal(task, active)
    assert programs.get_program(active['program_id'],user_id=OWNER)['queue'][1] == original_step
    admitted = accept_diagnosis(runtime, active, task)
    successor = admitted['queue'][1]
    assert successor['validation_parent_sequence'] == 1
    assert 'scientific_protocol' not in successor and 'scientific_required_outcome' not in successor
    assert {key:value for key,value in successor.items() if key not in ('validation_parent_sequence', 'failed_candidate_index')} == {
        key:value for key,value in original_step.items() if key not in ('scientific_protocol','scientific_required_outcome')}
    assert admitted['objective'] == active['objective'] and admitted['acceptance'] == active['acceptance']
    assert programs.experiment_page(active['program_id'],user_id=OWNER)['experiments'][0] == before[0]
    assert runtime[0].get_run(failed['run_id'],user_id=OWNER)['scientific_protocol'] == frozen


@pytest.mark.parametrize('change', ['candidate', 'same_candidate', 'threshold', 'baseline_generation', 'dataset', 'budget', 'budget_notation', 'outcome'])
def test_successor_preregistration_changes_only_candidate(runtime, failed_validation, change):
    active, original_step, frozen, task, failed = failed_validation
    admitted = accept_diagnosis(runtime, active, task)
    programs.advance_program(active['program_id'],user_id=OWNER)
    synthesis = programs.advance_program(active['program_id'],user_id=OWNER)
    current_task = runtime[2][-1]
    target = execute(current_task,{'action':'validation_context','program_id':active['program_id']},call_id='scope-context')['targets'][0]
    assert target['validation_parent_sequence'] == 1
    value = deepcopy(frozen);value['measurement_scope']['candidate_digest'] = 'e'*64
    required = 'supported'
    if change == 'same_candidate':value = deepcopy(frozen)
    elif change == 'threshold':value['decision_rule']['minimum_effect'] = -1
    elif change == 'baseline_generation':value['measurement_scope']['baseline_digest'] = 'e'*64
    elif change == 'dataset':value['measurement_scope']['dataset_digest'] = 'e'*64
    elif change == 'budget':value['measurement_scope']['budget'] += 1
    elif change == 'budget_notation':value['measurement_scope']['budget'] = float(value['measurement_scope']['budget'])
    elif change == 'outcome':required = 'refuted'
    args = {'action':'preregister','program_id':active['program_id'],'target_step_sha256':target['target_step_sha256'],
        'scientific_protocol':value,'scientific_required_outcome':required}
    if change not in ('candidate', 'budget_notation'):
        with pytest.raises(EvolutionStateError):execute(current_task,args,call_id='new-generation-freeze')
        assert 'scientific_protocol' not in programs.get_program(active['program_id'],user_id=OWNER)['queue'][1]
    else:
        registered = execute(current_task,args,call_id='new-generation-freeze')
        assert registered['scientific_protocol'] == value
        assert programs.validation_preregistered({'program_id':active['program_id'],'target_step_sha256':target['target_step_sha256']},
            synthesis['current']['run_id'],user_id=OWNER)
        if change == 'candidate':
            registered_step = programs.get_program(active['program_id'], user_id=OWNER)['queue'][1]
            encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
            growth = len(encoded(registered_step)) - len(encoded(original_step))
            assert 0 < growth < 256
            print(f'Validation successor step byte growth: {growth}')
            finish(runtime, current_task, 'The reviewed new candidate protocol has been registered.')
            from tests.evolution.test_evolution_hypothesis_review import assessment
            current_run = runtime[0].get_run(synthesis['current']['run_id'], user_id=OWNER)
            accepted = json.loads(review())
            accepted['hypothesis_assessment'] = assessment(current_run['exploration_hypothesis'],
                problem='pass', feasibility='pass', novelty='pass', net_value='pass')
            accepted['requirement_results'] = [{'requirement_id':f'requirement-{i+1}', 'outcome':'pass',
                'evidence_ref':'The synthetic reviewer approves the fixed-control generation protocol.'}
                for i in range(len(current_run['acceptance_requirements']))]
            finish(runtime, runtime[2][-1], json.dumps(accepted))
            for _ in range(3):
                launched = programs.advance_program(active['program_id'], user_id=OWNER)
            successor_run = runtime[0].get_run(launched['current']['run_id'], user_id=OWNER)
            assert successor_run['run_id'] != failed['run_id']
            assert successor_run['scientific_protocol'] == registered['scientific_protocol']
            assert successor_run['scientific_required_outcome'] == 'supported'
            assert runtime[0].get_run(failed['run_id'], user_id=OWNER)['scientific_protocol'] == frozen


def test_external_steps_cannot_forge_generation_parent(runtime):
    with pytest.raises(EvolutionContractError,match='lineage'):
        programs.create_program({'objective':'Forged generation','steps':[
            {'kind':'experiment','title':'Validate','instruction':'Measure','checks':['Passed'],
             'requires_scientific_protocol':True,'requires_frozen_measurement_scope':True,'validation_parent_sequence':1},
            {'kind':'final','title':'Audit','instruction':'Audit','checks':['Goal met']}]},
            user_id=OWNER,idempotency_key='forged-generation-parent')


def test_rejected_diagnosis_retains_original_frozen_validation(runtime, failed_validation):
    active, original_step, frozen, task, failed = failed_validation
    proposal(task, active)
    finish(runtime, task, 'The proposed changes are unverified.')
    judgment = json.loads(review())
    for row in judgment['criterion_results']:
        row['outcome'] = 'fail'
    current = runtime[0].get_run(active['current']['run_id'], user_id=OWNER)
    judgment['requirement_results'] = [{'requirement_id':f'requirement-{i+1}','outcome':'fail',
        'evidence_ref':'The candidate update has insufficient evidence.'} for i in range(len(current['acceptance_requirements']))]
    finish(runtime, runtime[2][-1], json.dumps(judgment))
    refused = programs.advance_program(active['program_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention' and refused['queue'][1] == original_step
    assert runtime[0].get_run(failed['run_id'], user_id=OWNER)['scientific_protocol'] == frozen


def test_refresh_cannot_admit_without_synthesis_or_confirmed_parent(runtime, failed_validation):
    active, original_step, frozen, task, failed = failed_validation
    with pytest.raises(EvolutionStateError, match='synthesis'):
        execute(task, {'program_id':active['program_id'],'action':'replan',
            'reason':'Candidate changes need a new generation.','steps':[candidate()],
            'refresh_validation_generation':True}, call_id='missing-synthesis')
    from lib.evolution import stage_outcomes, records
    parent_run = runtime[0].get_run(failed['run_id'], user_id=OWNER)
    execution = parent_run['receipts'][0]['task_id']
    metadata = stage_outcomes.read_metadata(failed['run_id'], execution, user_id=OWNER)
    metadata['scientific_audit']['failure_count'] = metadata['scientific_audit']['observation_count']
    metadata['scientific_audit']['outcomes'] = {'supported':0, 'refuted':0, 'inconclusive':0}
    records.put_record(stage_outcomes.NAMESPACE, stage_outcomes._key(execution, OWNER), metadata, kind='outcome')
    with pytest.raises(EvolutionStateError, match='numerical evidence'):
        proposal(task, active)
    assert 'replan_proposal' not in programs.get_program(active['program_id'], user_id=OWNER)


@pytest.mark.parametrize('generation_count', [3, 9])
def test_candidate_generations_keep_complete_failure_lineage(runtime, failed_validation, monkeypatch, generation_count):
    from tests.evolution.test_evolution_hypothesis_review import assessment
    active, original_step, original_protocol, diagnosis_task, first_failure = failed_validation
    failed_runs = [first_failure]
    protocols = [deepcopy(original_protocol)]
    archived_failures = [deepcopy(programs.experiment_page(active['program_id'], user_id=OWNER)['experiments'][0])]
    lineage_reads = []
    for generation in range(1, generation_count):
        candidate_digest = format(generation, '064x')
        accept_diagnosis(runtime, active, diagnosis_task)
        programs.advance_program(active['program_id'], user_id=OWNER)
        synthesis = programs.advance_program(active['program_id'], user_id=OWNER)
        executor = runtime[2][-1]
        target = execute(executor, {'action':'validation_context','program_id':active['program_id']}, call_id='next-scope')['targets'][0]
        assert target['validation_parent_sequence'] == archived_failures[-1]['sequence']
        if len(protocols) > 1:
            before = deepcopy(programs.get_program(active['program_id'], user_id=OWNER))
            with pytest.raises(EvolutionStateError, match='previously failed'):
                execute(executor, {'action':'preregister','program_id':active['program_id'],
                    'target_step_sha256':target['target_step_sha256'], 'scientific_protocol':protocols[0],
                    'scientific_required_outcome':'supported'}, call_id='cannot-recycle-first-failure')
            assert programs.get_program(active['program_id'], user_id=OWNER) == before
            from lib.evolution import records
            first = deepcopy(archived_failures[0])
            corrupt = deepcopy(first);corrupt['step']['validation_parent_sequence'] = first['sequence']
            key = programs._experiment_key(active['program_id'], first['sequence'], OWNER)
            records.put_record(programs.EXPERIMENTS, key, corrupt, kind='experiment')
            # Exercise the retained legacy path explicitly; indexed successors
            # intentionally do not rescan older immutable archive pages.
            legacy = deepcopy(before)
            legacy['queue'][1].pop('failed_candidate_index')
            header_key = records.entity_key(active['program_id'], OWNER, kind='program')
            records.put_record(programs.PROGRAMS, header_key, legacy, kind='program')
            legacy_target = execute(executor, {'action':'validation_context', 'program_id':active['program_id']}, call_id='legacy-context')['targets'][0]
            fresh = deepcopy(protocols[-1]);fresh['measurement_scope']['candidate_digest'] = candidate_digest
            with pytest.raises(EvolutionStateError, match='cyclic'):
                execute(executor, {'action':'preregister','program_id':active['program_id'],
                    'target_step_sha256':legacy_target['target_step_sha256'], 'scientific_protocol':fresh,
                    'scientific_required_outcome':'supported'}, call_id='corrupt-lineage-refused')
            records.put_record(programs.EXPERIMENTS, key, first, kind='experiment')
            records.put_record(programs.PROGRAMS, header_key, before, kind='program')
        protocol_value = deepcopy(protocols[-1])
        protocol_value['measurement_scope']['candidate_digest'] = candidate_digest
        page_calls = []
        original_page = programs.experiment_page
        def measured_page(*args, **kwargs):
            page_calls.append((kwargs.get('after'), kwargs.get('limit')))
            return original_page(*args, **kwargs)
        with monkeypatch.context() as measured:
            measured.setattr(programs, 'experiment_page', measured_page)
            registered = execute(executor, {'action':'preregister','program_id':active['program_id'],
                'target_step_sha256':target['target_step_sha256'],'scientific_protocol':protocol_value,
                'scientific_required_outcome':'supported'}, call_id='register-next-generation')
        lineage_reads.append({'failed_generations': len(protocols), 'archive_page_calls': len(page_calls)})
        protocol_value = registered['scientific_protocol']
        finish(runtime, executor, 'The next candidate is registered under unchanged controls.')
        run = runtime[0].get_run(synthesis['current']['run_id'], user_id=OWNER)
        judged = json.loads(review())
        judged['hypothesis_assessment'] = assessment(run['exploration_hypothesis'],
            problem='pass', feasibility='pass', novelty='pass', net_value='pass')
        judged['requirement_results'] = [{'requirement_id':f'requirement-{i+1}','outcome':'pass',
            'evidence_ref':'Synthetic review approves the fixed-control successor.'} for i in range(len(run['acceptance_requirements']))]
        finish(runtime, runtime[2][-1], json.dumps(judged))
        for _ in range(3):
            validation = programs.advance_program(active['program_id'], user_id=OWNER)
        new_run = runtime[0].get_run(validation['current']['run_id'], user_id=OWNER)
        assert new_run['scientific_protocol'] == protocol_value
        packet = observation_payload(protocol_value, 'metrics', 'refuted')
        packet['scientific_observations']['comparison']['method']['system_digest'] = candidate_digest
        packet['scientific_observations']['comparison']['baseline']['system_digest'] = original_protocol['measurement_scope']['baseline_digest']
        runtime[2][-1].update(command(packet))
        failed = finish(runtime, runtime[2][-1], 'The new candidate still misses the target.')
        assert failed['error'] == 'scientific_outcome_requirement_unmet'
        failed_runs.append(failed);protocols.append(deepcopy(protocol_value))
        queued = programs.advance_program(active['program_id'], user_id=OWNER)
        history = []
        cursor = 0
        while True:
            page = programs.experiment_page(active['program_id'], user_id=OWNER, after=cursor)
            history.extend(page['experiments'])
            if page['next_cursor'] is None:
                break
            cursor = page['next_cursor']
        for saved in archived_failures:
            assert next(row for row in history if row['sequence'] == saved['sequence']) == saved
        archived_failures.append(deepcopy(history[-1]))
        if generation == generation_count - 1:
            # A legacy chain can acquire the same membership without trusting
            # absent indexes or changing any archived validation controls.
            from lib.evolution.validation_candidate_index import FailedCandidates
            legacy_parent = deepcopy(archived_failures[-1])
            legacy_parent['step'].pop('failed_candidate_index')
            rebuilt = programs._prepare_failed_candidate_index(queued, legacy_parent, user_id=OWNER)
            cold = FailedCandidates(runtime[1], user_id=OWNER, program_id=active['program_id'], step=legacy_parent['step'])
            assert rebuilt['count'] == len(protocols)
            for frozen in protocols:
                assert cold.contains(rebuilt, frozen['measurement_scope']['candidate_digest'])
        assert queued['objective'] == active['objective'] and queued['acceptance'] == active['acceptance']
        assert queued['queue'][0]['recovery_of'] == archived_failures[-1]['sequence']
        programs.advance_program(active['program_id'], user_id=OWNER)
        active = programs.advance_program(active['program_id'], user_id=OWNER)
        diagnosis_task = runtime[2][-1]
    assert len({run['run_id'] for run in failed_runs}) == generation_count
    print('validation lineage preregistration reads:', json.dumps(lineage_reads))
    assert max(row['archive_page_calls'] for row in lineage_reads) <= 2
    for run, frozen in zip(failed_runs, protocols):
        assert runtime[0].get_run(run['run_id'], user_id=OWNER)['scientific_protocol'] == frozen


@pytest.mark.parametrize('failed_validation', [1,0], indirect=True)
@pytest.mark.parametrize('history_delivery', ['unread','partial','clipped','complete','legacy','missing_parent_ref','cancel_confirmation'])
def test_successor_review_receives_owned_parent_reuse_facts(runtime, failed_validation, history_delivery, monkeypatch):
    from tests.evolution.test_evolution_hypothesis_review import assessment
    active, original_step, frozen, task, failed = failed_validation
    accept_diagnosis(runtime, active, task)
    programs.advance_program(active['program_id'], user_id=OWNER)
    synthesis = programs.advance_program(active['program_id'], user_id=OWNER)
    executor = runtime[2][-1]
    target = execute(executor, {'action':'validation_context','program_id':active['program_id']}, call_id='scope')['targets'][0]
    value = deepcopy(frozen);value['measurement_scope']['candidate_digest'] = 'e'*64
    execute(executor, {'action':'preregister','program_id':active['program_id'],
        'target_step_sha256':target['target_step_sha256'],'scientific_protocol':value,
        'scientific_required_outcome':'supported'}, call_id='freeze')
    finish(runtime,executor,'Prepared changed candidate under unchanged controls.')
    run = runtime[0].get_run(synthesis['current']['run_id'],user_id=OWNER)
    judgment = json.loads(review())
    judgment['hypothesis_assessment'] = assessment(run['exploration_hypothesis'],
        problem='pass',feasibility='pass',novelty='pass',net_value='pass')
    judgment['requirement_results'] = [{'requirement_id':f'requirement-{i+1}','outcome':'pass',
        'evidence_ref':'Synthetic synthesis approval.'} for i in range(len(run['acceptance_requirements']))]
    finish(runtime,runtime[2][-1],json.dumps(judgment))
    for _ in range(3): validation = programs.advance_program(active['program_id'],user_id=OWNER)
    current = runtime[0].get_run(validation['current']['run_id'],user_id=OWNER)
    packet = observation_payload(value,'metrics','supported')
    packet['scientific_observations']['comparison']['method']['system_digest'] = 'e'*64
    packet['scientific_observations']['comparison']['baseline']['system_digest'] = frozen['measurement_scope']['baseline_digest']
    runtime[2][-1].update(command(packet))
    executor = runtime[2][-1]
    content = 'New candidate reports a supported result.'
    executor.update(status='done',finishReason='stop',content=content,finished_at=executor['created_at']+1,
        usage={'input_tokens':10,'output_tokens':5})
    checkpoint = executor['_terminalDelivery'].prepare_persistence(executor,content,0)
    frozen_prompt = checkpoint['outcome']['review_prompt']
    frozen_payload = json.JSONDecoder().raw_decode(frozen_prompt[frozen_prompt.rfind('\n{"objective":')+1:])[0]
    assert frozen_payload['scientific_protocol'] == value
    assert frozen_payload['validation_feedback_reuse']['parent_run_id'] == failed['run_id']
    assert json.loads(frozen_payload['frozen_executor_context'])['program_id'] == active['program_id']
    from lib.tasks_pkg.manager import _persist
    from lib.agent_core.execution_session import ExecutionPhase
    executor['_executionSession'].settle(ExecutionPhase.COMPLETED,cause='controlled_successor')
    assert _persist.persist_task_result(executor,_defer_heavy_release=True) is True
    reviewing = runtime[0].get_run(current['run_id'],user_id=OWNER)
    assert reviewing['status'] == 'reviewing' and reviewing['evidence_review_version'] == 3
    prompt = runtime[2][-1]['messages'][0]['content']
    payload = json.JSONDecoder().raw_decode(prompt[prompt.rfind('\n{"objective":')+1:])[0]
    facts = payload['validation_feedback_reuse']
    assert facts['parent_sequence'] == 1 and facts['parent_run_id'] == failed['run_id']
    assert facts['parent_candidate_digest'] == frozen['measurement_scope']['candidate_digest']
    assert facts['current_candidate_digest'] == 'e'*64
    assert facts['same_dataset_digest'] and facts['same_split_digest']
    parent = programs.experiment_page(active['program_id'],user_id=OWNER,limit=1)['experiments'][0]
    assert facts['parent_sources']['tool_evidence_source'] == parent['tool_evidence_source']
    assert 'do not establish actual data use' in facts['evidence_status']
    assert 'changed candidate digest cannot' in prompt
    # A non-current run cannot manufacture a parent fact through context text.
    spoofed = deepcopy(current);spoofed['run_id'] = failed['run_id']
    assert programs.validation_generation_review_input(spoofed,user_id=OWNER) == {}

    changed = deepcopy(current)
    changed['scientific_protocol']['measurement_scope']['candidate_digest'] = 'f'*64
    with pytest.raises(EvolutionStateError, match='frozen protocol changed'):
        programs.validation_generation_review_input(changed,user_id=OWNER)
    with pytest.raises(EvolutionStateError):
        programs.validation_generation_review_input(current,user_id=OWNER+1)

    from tests.evolution.test_evolution_review_delivery import read_originals
    from tests.evolution.test_evolution_source_tools import delivered
    from lib.evolution.source_tools import execute as source_execute, history_read_complete
    reviewer = runtime[2][-1]
    assert source_execute(reviewer, {'action':'list'})['review_next_action'] == 'read_current_sources'
    read_originals(reviewing, reviewer)
    if history_delivery == 'missing_parent_ref':
        missing = deepcopy(parent)
        for key in facts['parent_sources']: missing.pop(key)
        programs.records.put_record(programs.EXPERIMENTS,
            programs._experiment_key(active['program_id'],1,OWNER), missing, kind='experiment')
    source_list = source_execute(reviewer, {'action':'list'})
    assert not source_list['historical_read_progress']['all_sources_delivered']
    assert source_list['review_next_action'] == ('submit_unknown_review' if history_delivery == 'missing_parent_ref' else 'read_historical_sources')
    if history_delivery == 'cancel_confirmation':
        from lib.evolution import source_tools
        source = facts['parent_sources']['tool_evidence_source']
        args = {'action':'read','program_id':active['program_id'],'sequence':1,'run_id':failed['run_id'],
            'task_id':source['task_id'],'source_sha256':source['source_sha256'],'source_kind':'tool_evidence'}
        entry = source_tools.handle(reviewer,None,'evolution_source','late-history',args,1,{},reviewer['config'],'',False)[0]
        confirmation = source_tools.prepare_model_input(reviewer,{'messages':[{'role':'tool','tool_call_id':'late-history',
            'content':entry['title']+': '+entry['snippet']}]})
        original = source_tools.review_history_sources
        def cancelled_during_validation(*args, **kwargs):
            expected = original(*args, **kwargs)
            record, version = runtime[0]._read(reviewing['run_id'],OWNER)
            record['status'] = 'cancelled';runtime[0]._save(record,version,OWNER)
            return expected
        before = deepcopy(reviewer.get('_evolutionToolEvidenceCoverage'))
        monkeypatch.setattr(source_tools,'review_history_sources',cancelled_during_validation)
        with pytest.raises(EvolutionStateError): confirmation()
        assert not reviewer.get('_evolutionHistoricalSourceCoverage')
        assert not reviewer.get('_evolutionSourcePendingReads')
        assert reviewer.get('_evolutionToolEvidenceCoverage') == before
        return
    for key, source in facts['parent_sources'].items():
        kind = {'result_source':'deliverable','tool_evidence_source':'tool_evidence',
            'artifact_evidence_source':'artifact_evidence','background_evidence_source':'background_evidence'}[key]
        if history_delivery in ('unread','legacy','missing_parent_ref'): continue
        offset = 0
        while True:
            chunk = delivered(reviewer, {'action':'read','program_id':active['program_id'],'sequence':1,
                'run_id':failed['run_id'],'task_id':source['task_id'],'source_sha256':source['source_sha256'],
                'source_kind':kind,'offset':offset, **({'limit':20} if history_delivery == 'partial' else {})},
                call_id='history-'+kind+str(offset),
                model_projection=(lambda text:text[:30]) if history_delivery == 'clipped' else None)
            if history_delivery != 'complete' or chunk['next_offset'] is None: break
            offset = chunk['next_offset']
    assert history_read_complete(reviewer, reviewing, owner=OWNER) == (history_delivery == 'complete')
    expected_action = 'submit_independent_review' if history_delivery == 'complete' else 'submit_unknown_review' if history_delivery == 'missing_parent_ref' else 'read_historical_sources'
    assert source_execute(reviewer, {'action':'list'})['review_next_action'] == expected_action
    authored = json.loads(review())
    authored['requirement_results'] = [{'requirement_id':f'requirement-{i+1}','outcome':'pass',
        'evidence_ref':'Synthetic numerical claim.'} for i in range(len(reviewing['acceptance_requirements']))]
    checked = deepcopy(authored)
    policy_run = {**reviewing, **({'evidence_review_version':1} if history_delivery == 'legacy' else {})}
    runtime[0]._enforce_review_evidence(reviewer, policy_run, checked)
    objective = next(row for row in checked['criterion_results'] if row['criterion_id'] == 'objective')
    assert objective['outcome'] == ('pass' if history_delivery in ('complete','legacy') else 'unknown')
    if history_delivery in ('unread','partial','clipped','missing_parent_ref'):
        from lib.evolution import review_delivery
        args = {'review':authored}
        reviewer['_compact_messages'] = [{'role':'assistant','content':'','tool_calls':[{'id':'review-call','type':'function',
            'function':{'name':'evolution_review','arguments':json.dumps(args)}}]}]
        with pytest.raises(EvolutionStateError, match='inspection is unfinished'):
            review_delivery.prepare(reviewer,args,call_id='review-call',round_num=0)
        assert '_explicitToolDeliveryRequest' not in reviewer
    if history_delivery == 'missing_parent_ref':
        from tests.evolution.test_evolution_review_delivery import acknowledge
        unknown = deepcopy(authored)
        unknown['evidence_audit']['complete'] = False
        for row in unknown['criterion_results']:
            row.update(outcome='unknown',evidence_ref='Required parent original references are unavailable; current originals were inspected.')
        unknown_args = {'review':unknown}
        message = {'role':'assistant','content':'','tool_calls':[{'id':'review-call','type':'function',
            'function':{'name':'evolution_review','arguments':json.dumps(unknown_args)}}]}
        reviewer['_compact_messages'] = [message]
        result = review_delivery.prepare(reviewer,unknown_args,call_id='review-call',round_num=0)
        assert acknowledge(reviewer,message,result)
        count = len(runtime[2])
        settled = finish(runtime,reviewer,reviewer['content'])
        assert settled['max_attempts'] in (0,1)
        assert settled['max_attempts'] == active['execution_options']['max_attempts']
        assert settled['status'] == 'needs_attention'
        assert settled['error'] == 'validation_parent_originals_unavailable'
        assert settled['review']['outcome'] != 'success'
        assert len(runtime[2]) == count, 'Unavailable parent originals must not trigger executor replay'
        assert not reviewer.get('_evolutionHistoricalSourceCoverage')
    if history_delivery == 'complete':
        from lib.evolution import stage_outcomes
        settled = finish(runtime,reviewer,json.dumps(authored))
        assert settled['status'] == 'completed'
        metadata = stage_outcomes.read_metadata(settled['run_id'],reviewer['id'],user_id=OWNER)
        assert metadata['historical_source_coverage'] == reviewer['_evolutionHistoricalSourceCoverage']
        assert len(json.dumps(metadata['historical_source_coverage']).encode()) < 4096
        _, restored = stage_outcomes.recover({**settled,'task_id':reviewer['id']},user_id=OWNER)
        assert restored['_evolutionHistoricalSourceCoverage'] == reviewer['_evolutionHistoricalSourceCoverage']


def test_successor_cannot_weaken_inherited_observation_format_by_omitting_field(runtime, failed_validation):
    active, original_step, frozen, task, failed = failed_validation
    admitted = accept_diagnosis(runtime, active, task)
    programs.advance_program(active['program_id'], user_id=OWNER)
    synthesis = programs.advance_program(active['program_id'], user_id=OWNER)
    current_task = runtime[2][-1]
    target = execute(current_task, {'action':'validation_context','program_id':active['program_id']}, call_id='identified-successor-context')['targets'][0]
    value = deepcopy(frozen); value['measurement_scope']['candidate_digest'] = 'e'*64
    value.pop('observation_format', None)
    registered = execute(current_task, {'action':'preregister','program_id':active['program_id'],
        'target_step_sha256':target['target_step_sha256'], 'scientific_protocol':value,
        'scientific_required_outcome':'supported'}, call_id='identified-successor-freeze')
    assert registered['scientific_protocol'].get('observation_format') == frozen.get('observation_format')
    assert programs.validation_preregistered({'program_id':active['program_id'], 'target_step_sha256':target['target_step_sha256']},
                                          synthesis['current']['run_id'], user_id=OWNER)
    assert runtime[0].get_run(failed['run_id'], user_id=OWNER)['scientific_protocol'] == frozen
