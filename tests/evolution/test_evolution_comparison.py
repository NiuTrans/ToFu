"""Paired evidence must not turn missing observations into evolution claims."""
import json

import pytest

from lib.evolution.comparison import compare_pairs
from lib.evolution.contracts import EvolutionContractError

pytestmark = pytest.mark.unit


def arm(outcome, tokens=None):
    return {'outcome': outcome, 'tokens': tokens, 'wall_clock_seconds': None}


def pairs(count=3, baseline='fail', candidate='pass'):
    return [{'item_id': str(i), 'baseline': arm(baseline), 'candidate': arm(candidate)}
            for i in range(count)]


def test_gain_is_observed_with_sample_uncertainty_and_unknown_cost():
    report = compare_pairs(pairs())
    assert report['conclusion'] == 'observed_gain'
    assert report['sign_test_p'] == 0.125
    assert report['wins'] == 3 and report['losses'] == 0
    assert report['pairs'][0]['candidate']['tokens'] is None
    assert compare_pairs(pairs(2))['conclusion'] == 'inconclusive'
    assert compare_pairs(pairs(3, 'pass'))['conclusion'] == 'no_gain'


def test_missing_cases_cannot_hide_regression_or_claim_gain():
    sample = pairs()
    sample[-1]['candidate'] = arm('skip')
    assert compare_pairs(sample)['conclusion'] == 'inconclusive'
    sample[0].update(baseline=arm('pass'), candidate=arm('fail'))
    assert compare_pairs(sample)['conclusion'] == 'regression'


def test_invalid_or_duplicate_observations_rejected():
    with pytest.raises(EvolutionContractError):
        compare_pairs(pairs() * 2)
    sample = pairs()
    sample[0]['candidate']['wall_clock_seconds'] = float('nan')
    with pytest.raises(EvolutionContractError):
        compare_pairs(sample)


def test_eval_uses_matched_inputs_counterbalances_and_never_leaks_answers():
    from evaluations.evolution.paired import evaluate
    calls = []
    cases = [{'id': str(i), 'objective': f'task {i}', 'expected': {'value': i}}
             for i in range(3)]
    def invoke(messages, **kwargs):
        calls.append(messages)
        assert 'expected' not in json.dumps(messages)
        assert 0 < kwargs['timeout_s'] <= 60
        i = int(messages[-1]['content'].split()[-1])
        return {'status': 'completed', 'content': json.dumps({'value': i}), 'model':'fixed',
                'usage': {'input_tokens': 10, 'output_tokens': 2}}
    result = evaluate(cases, 'baseline', 'candidate', invoke=invoke)
    assert [c[0]['content'] for c in calls] == [
        'baseline', 'candidate', 'candidate', 'baseline', 'baseline', 'candidate']
    assert result['comparison']['conclusion'] == 'no_gain'
    assert result['comparison']['pairs'][0]['baseline']['tokens'] == 12
    def unknown_usage(messages, **kwargs):
        return {**invoke(messages, **kwargs),
                'usage': {'input_tokens': None, 'output_tokens': None}}
    unknown = evaluate(cases, 'baseline', 'candidate', invoke=unknown_usage)
    assert unknown['costs']['baseline']['tokens']['total'] is None


def test_eval_provider_failure_is_unknown_and_secret_free():
    from evaluations.evolution.paired import evaluate, exact_json
    def fail(*args, **kwargs):
        raise RuntimeError('private-api-key')
    cases = [{'id': str(i), 'objective': 'return JSON', 'expected': True} for i in range(3)]
    result = evaluate(cases, 'baseline', 'candidate', invoke=fail)
    assert result['comparison']['measured'] == 0
    assert 'private-api-key' not in json.dumps(result)
    assert exact_json('1', True) == 'fail'
    assert exact_json('NaN', 0) == 'fail'


def test_cli_preflight_is_offline_and_reproducible(tmp_path):
    from evaluations.evolution.paired import main
    cases = tmp_path / 'cases.json'
    cases.write_text(json.dumps([{'id': str(i), 'objective': 'return []', 'expected': []}
                                 for i in range(3)]))
    baseline, candidate, output = [tmp_path / n for n in ('before.txt', 'after.txt', 'out.json')]
    baseline.write_text('baseline')
    candidate.write_text('candidate')
    assert main(['--cases', str(cases), '--baseline', str(baseline), '--candidate',
                 str(candidate), '--owner', '123', '--output', str(output)]) == 0
    assert json.loads(output.read_text())['provider_called'] is False


def test_model_drift_invalidates_quality_comparison_and_missing_usage_stays_unknown():
    from evaluations.evolution.paired import evaluate
    cases = [{'id': str(i), 'objective': 'return []', 'expected': []} for i in range(3)]
    def invoke(messages, **kwargs):
        return {'status': 'completed', 'content': '[]', 'model': messages[0]['content']}
    result = evaluate(cases, 'model-a', 'model-b', invoke=invoke)
    assert result['route_consistent'] is False
    assert result['comparison']['conclusion'] == 'inconclusive'
    assert result['comparison']['measured'] == 0
    assert result['costs']['baseline']['tokens']['total'] is None


def test_exhausted_total_budget_stops_new_calls(monkeypatch):
    from evaluations.evolution import paired
    tick = iter([0, 601, 602, 603, 604, 605, 606, 607])
    monkeypatch.setattr(paired.time, 'monotonic', lambda: next(tick))
    cases = [{'id': str(i), 'objective': 'return []', 'expected': []} for i in range(3)]
    result = paired.evaluate(cases, 'baseline', 'candidate',
                             invoke=lambda *_args, **_kwargs: pytest.fail('budget exhausted'))
    assert result['comparison']['measured'] == 0
    assert all(o['error'] == 'budget_exhausted' for o in result['observations'])


def test_chat_evaluation_reuses_authenticated_catalog_and_goal_role():
    from evaluations.evolution.chat_access import ChatEvaluator
    selected={'creator_id':'test','model_id':'chat'}
    review={'creator_id':'test','model_id':'review'}
    calls=[]
    def request(path,body=None,**kw):
        calls.append((path,body))
        if path == '/api/health': return {'bootId':'stable-boot'}
        if path.endswith('whoami'):return {'authenticated':True,'owner_id':73}
        if path.endswith('/models'):return {'models':[{'model':selected},{'model':review}]}
        if '/runs/' in path:return {'run':{'model':selected,'role_models':{'reviewer':review}}}
        assert body['model']==review
        assert body['config']['toolSpecAllowlist']==[]
        return {'model':'review','choices':[{'message':{'content':'[]'},'finish_reason':'stop'}]}
    client=ChatEvaluator(user_id=73,run_id='goal',role='reviewer',request=request)
    assert client.model==review
    assert client.invoke([{'role':'user','content':'return []'}],timeout_s=10)['status']=='done'
    with pytest.raises(ValueError,match='owner'):
        ChatEvaluator(user_id=74,request=request)


def test_paired_scoring_refuses_ambiguous_objects_and_keeps_completed_call_usage():
    from evaluations.evolution.paired import evaluate, exact_json
    assert exact_json('{"answer":false,"answer":true}', {'answer':True}) == 'fail'
    assert exact_json('{"nested":{"answer":false,"answer":true}}', {'nested':{'answer':True}}) == 'fail'
    content = '[' * 10000 + '0' + ']' * 10000
    cases = [{'id':str(i), 'objective':'return JSON', 'expected':[]} for i in range(3)]
    def invoke(*args, **kwargs):
        return {'status':'completed', 'content':content, 'model':'fixed',
                'usage':{'input_tokens':10, 'output_tokens':20}}
    result = evaluate(cases, 'baseline', 'candidate', invoke=invoke)
    assert result['comparison']['measured'] == 3
    assert exact_json(content, []) == 'fail'
    for arm in ('baseline', 'candidate'):
        assert result['costs'][arm]['tokens'] == {'observed_cases':3, 'total':90}
    assert all(row['status'] == 'completed' for row in result['observations'])


@pytest.mark.parametrize('bad', ['objective', 'expected', 'nested', 'deep'])
def test_frozen_evaluation_cases_refuse_ambiguous_json_before_model_access(tmp_path, bad):
    from evaluations.evolution.paired import read_cases
    rows = [{'id':str(i), 'objective':'return JSON', 'expected':True} for i in range(3)]
    raw = json.dumps(rows)
    if bad == 'objective': raw = raw.replace('"objective": "return JSON"', '"objective":"other", "objective":"return JSON"', 1)
    elif bad == 'expected': raw = raw.replace('"expected": true', '"expected":false, "expected":true', 1)
    elif bad == 'nested': raw = raw.replace('"expected": true', '"expected":{"answer":false,"answer":true}', 1)
    else: raw = raw.replace('"expected": true', '"expected":' + '[' * 10000 + '0' + ']' * 10000, 1)
    path = tmp_path / 'cases.json'
    path.write_text(raw)
    with pytest.raises(ValueError): read_cases(path)


@pytest.mark.parametrize('change', ['missing_answer', 'bad_objective', 'nan', 'oversized'])
def test_direct_evaluation_intake_refuses_invalid_cases_before_invocation(change):
    from evaluations.evolution.paired import evaluate
    cases = [{'id':str(i), 'objective':'return true', 'expected':True} for i in range(3)]
    if change == 'missing_answer': cases[1].pop('expected')
    elif change == 'bad_objective': cases[1]['objective'] = None
    elif change == 'nan': cases[1]['expected'] = float('nan')
    else: cases[1]['expected'] = 'x' * 131073
    def forbidden(*args, **kwargs): pytest.fail('Invalid cases must fail before model invocation')
    with pytest.raises(ValueError): evaluate(cases, 'baseline', 'candidate', invoke=forbidden)


def test_evaluation_freezes_answers_before_caller_mutates_cases():
    from evaluations.evolution.paired import evaluate, digest
    cases = [{'id':str(i), 'objective':'return true', 'expected':True} for i in range(3)]
    identity = digest(cases)
    messages_seen = []
    def invoke(messages, **kwargs):
        messages_seen.append(messages)
        for row in cases:
            row['expected'] = False
            row['objective'] = 'changed during invocation'
        return {'status':'completed', 'content':'true', 'model':'fixed',
                'usage':{'input_tokens':1, 'output_tokens':1}}
    result = evaluate(cases, 'baseline', 'candidate', invoke=invoke)
    assert result['cases_sha256'] == identity
    assert all(messages[1]['content'] == 'return true' for messages in messages_seen)
    assert result['comparison']['measured'] == 3
    assert all(pair[arm]['outcome'] == 'pass' for pair in result['comparison']['pairs']
               for arm in ('baseline', 'candidate'))


@pytest.mark.parametrize('usage', [
    {'input_tokens':10, 'prompt_tokens':'invalid', 'output_tokens':20},
    {'input_tokens':10, 'prompt_tokens':True, 'output_tokens':20},
    {'input_tokens':10, 'prompt_tokens':-1, 'output_tokens':20},
    {'input_tokens':10, 'output_tokens':20, 'completion_tokens':None},
    ['not', 'a', 'usage', 'object'],
])
def test_malformed_usage_aliases_remain_unknown_without_erasing_quality(usage):
    from evaluations.evolution.paired import evaluate
    cases = [{'id':str(i), 'objective':'return true', 'expected':True} for i in range(3)]
    result = evaluate(cases, 'baseline', 'candidate', invoke=lambda *args, **kwargs:
                      {'status':'completed', 'content':'true', 'model':'fixed', 'usage':usage})
    assert result['comparison']['measured'] == 3
    for pair in result['comparison']['pairs']:
        for arm in ('baseline', 'candidate'):
            assert pair[arm]['outcome'] == 'pass' and pair[arm]['tokens'] is None
    for arm in ('baseline', 'candidate'):
        assert result['costs'][arm]['tokens']['total'] is None


def test_preflight_and_direct_evaluation_pin_the_same_scoring_identity(tmp_path):
    from evaluations.evolution.paired import main, evaluate
    cases = [{'id':str(i), 'objective':'return true', 'expected':True} for i in range(3)]
    path = tmp_path / 'cases.json'; path.write_text(json.dumps(cases))
    baseline = tmp_path / 'baseline.txt'; baseline.write_text('baseline')
    candidate = tmp_path / 'candidate.txt'; candidate.write_text('candidate')
    output = tmp_path / 'report.json'
    assert main(['--cases',str(path), '--baseline',str(baseline), '--candidate',str(candidate),
                 '--output',str(output), '--owner','123']) == 0
    preflight = json.loads(output.read_text())
    report = evaluate(cases, 'baseline', 'candidate', invoke=lambda *args, **kwargs:
                      {'status':'completed', 'content':'true', 'model':'fixed'})
    assert preflight['provider_called'] is False
    assert report['harness_version'] == preflight['harness_version'] == 2
    for key in ('harness_sha256', 'comparison_sha256', 'comparison_contract_sha256'):
        assert len(report[key]) == 64 and report[key] == preflight[key]


@pytest.mark.parametrize('missing', ['all', 'one', 'none'])
def test_unknown_model_identity_cannot_be_claimed_as_consistent_route(missing):
    from evaluations.evolution.paired import evaluate
    calls = [0]
    cases = [{'id':str(i), 'objective':'return true', 'expected':True} for i in range(3)]
    def invoke(*args, **kwargs):
        calls[0] += 1
        model = {} if missing == 'all' or (missing == 'one' and calls[0] == 1) else {'creator_id':'test','model_id':'fixed'}
        return {'status':'completed', 'content':'true', 'model':model,
                'usage':{'input_tokens':1, 'output_tokens':1}}
    result = evaluate(cases, 'baseline', 'candidate', invoke=invoke)
    assert result['route_consistent'] is (True if missing == 'none' else None)
    assert result['comparison']['measured'] == (3 if missing == 'none' else 0)
    assert all(row['scored_outcome'] == 'pass' for row in result['observations'])
    assert result['costs']['baseline']['tokens']['total'] == 6


def test_chat_evaluator_requests_json_object_only_when_explicit():
    from evaluations.evolution.chat_access import ChatEvaluator
    model = {'creator_id':'test','model_id':'fixed'}
    calls = []
    def request(path, body=None, **kwargs):
        if path == '/api/health': return {'bootId':'stable-boot'}
        if path.endswith('whoami'): return {'authenticated':True,'owner_id':123}
        if path.endswith('/models'): return {'models':[{'model':model}]}
        calls.append(body)
        return {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}], 'model':model}
    for enabled in (False, True):
        ChatEvaluator(user_id=123, request=request, json_object=enabled).invoke([], timeout_s=1)
    assert 'response_format' not in calls[0]
    assert calls[1]['response_format'] == {'type':'json_object'}


def test_json_object_cli_refuses_nonobject_answers_before_model_access(tmp_path):
    from evaluations.evolution.paired import main
    cases = tmp_path / 'cases.json'
    cases.write_text(json.dumps([{'id':str(i),'objective':'return true','expected':True} for i in range(3)]))
    with pytest.raises(SystemExit):
        main(['--cases',str(cases),'--baseline',str(tmp_path/'missing-before'),
              '--candidate',str(tmp_path/'missing-after'),'--output',str(tmp_path/'out'),
              '--owner','123','--json-object'])


@pytest.mark.parametrize('answer,expected,outcome', [
    ('{"mean":1.0}', {'mean':1}, 'pass'),
    ('true', 1, 'fail'), ('1', True, 'fail'),
    ('[1,2]', [2,1], 'fail'), ('1.00001', 1, 'fail'),
    ('{"mean":1,"extra":0}', {'mean':1}, 'fail'),
    ('{"mean":1,"mean":1}', {'mean':1}, 'fail'),
    ('NaN', float('nan'), 'fail'), ('Infinity', float('inf'), 'fail'),
    ('1 followed by explanation', 1, 'fail'),
])
def test_numeric_checker_preserves_strict_scientific_delivery_boundaries(answer, expected, outcome):
    from evaluations.evolution.paired import json_numeric_equivalent
    assert json_numeric_equivalent(answer, expected) == outcome


def test_checker_choice_is_explicit_and_reported_before_any_provider_call():
    from evaluations.evolution.paired import evaluate
    cases = [{'id':str(i), 'objective':'Return number one.', 'expected':1} for i in range(3)]
    def invoke(*args, **kwargs):
        return {'status':'done', 'content':'1.0', 'model':'fixed', 'usage':{}}
    strict = evaluate(cases, 'baseline', 'candidate', invoke=invoke)
    numeric = evaluate(cases, 'baseline', 'candidate', invoke=invoke,
                       checker='json_numeric_equivalent_v1')
    assert strict['comparison']['pairs'][0]['baseline']['outcome'] == 'fail'
    assert numeric['comparison']['pairs'][0]['baseline']['outcome'] == 'pass'
    assert strict['checker'] != numeric['checker']
    with pytest.raises(ValueError, match='unknown evaluation checker'):
        evaluate(cases, 'baseline', 'candidate', invoke=lambda *_: pytest.fail('must not invoke'),
                 checker='unsupported')


def test_paired_report_keeps_exact_answers_for_offline_failure_audit():
    from evaluations.evolution.paired import evaluate, digest
    cases = [{'id':str(i), 'objective':'Return true.', 'expected':True} for i in range(3)]
    original = 'false\nUnexpected prose 🧪'
    result = evaluate(cases, 'baseline', 'candidate', invoke=lambda *args, **kwargs:
                      {'status':'done', 'content':original, 'model':'fixed', 'usage':{}})
    for row in result['observations']:
        assert row['output_original'] == original
        assert row['output_sha256'] == digest(row['output_original'])
        assert row['output_retained'] is True
        assert row['output_bytes'] == len(original.encode('utf-8'))
        assert row['scored_outcome'] == 'fail'


def test_answer_retention_bound_does_not_truncate_or_erase_measured_cost():
    from evaluations.evolution.paired import evaluate, MAX_RETAINED_ANSWER_BYTES
    cases = [{'id':str(i), 'objective':'Return true.', 'expected':True} for i in range(3)]
    result = evaluate(cases, 'baseline', 'candidate', invoke=lambda *args, **kwargs:
        {'status':'done', 'content':'🧪' * (MAX_RETAINED_ANSWER_BYTES // 4 + 1),
         'model':'fixed', 'usage':{'input_tokens':10, 'output_tokens':20}})
    for row in result['observations']:
        assert row['output_retained'] is False
        assert row['output_retention_reason'] == 'answer_exceeds_64_kib'
        assert 'output_original' not in row
    assert result['costs']['candidate']['tokens']['total'] == 90


def test_completed_call_checkpoint_survives_interruption_without_replay(tmp_path):
    from evaluations.evolution.paired import evaluate, write_report
    path = tmp_path / 'checkpoint.json'
    calls = []
    cases = [{'id':str(i), 'objective':'Return true.', 'expected':True} for i in range(3)]
    def invoke(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt('process interrupted')
        return {'status':'done','content':'true','model':'fixed',
                'task_id':'original-call','usage':{'input_tokens':10,'output_tokens':20}}
    def checkpoint(observations):
        write_report(path, {'evaluation_state':'running','observations':observations})
    with pytest.raises(KeyboardInterrupt):
        evaluate(cases, 'baseline', 'candidate', invoke=invoke, on_observation=checkpoint)
    saved = json.loads(path.read_text())
    assert len(saved['observations']) == 1 and len(calls) == 2
    row = saved['observations'][0]
    assert row['output_original'] == 'true' and row['tokens'] == 30
    assert saved['evaluation_state'] == 'running' and 'comparison' not in saved


def test_failed_checkpoint_stops_new_calls_and_keeps_previous_report(tmp_path, monkeypatch):
    import os
    from evaluations.evolution.paired import evaluate, write_report
    path = tmp_path / 'checkpoint.json'
    write_report(path, {'evaluation_state':'running','observations':[]})
    old = path.read_bytes()
    monkeypatch.setattr(os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('write failed')))
    calls = []
    def invoke(*args, **kwargs):
        calls.append(1)
        return {'status':'done','content':'true','model':'fixed','usage':{}}
    cases = [{'id':str(i), 'objective':'Return true.', 'expected':True} for i in range(3)]
    with pytest.raises(OSError, match='write failed'):
        evaluate(cases,'baseline','candidate',invoke=invoke,
                 on_observation=lambda rows: write_report(path, {'observations':rows}))
    assert len(calls) == 1 and path.read_bytes() == old
    assert list(tmp_path.iterdir()) == [path]


def test_request_intent_survives_interrupt_before_any_reply(tmp_path):
    from evaluations.evolution.paired import evaluate, write_report
    path = tmp_path / 'checkpoint.json'
    cases = [{'id':str(i), 'objective':'Return true.', 'expected':True} for i in range(3)]
    def checkpoint(request, rows):
        write_report(path, {'evaluation_state':'running','inflight':request,
                           'observations':rows,'provider_called':None})
    def invoke(*args, **kwargs):
        saved = json.loads(path.read_text())
        assert saved['inflight'] == {'item_id':'0','arm':'baseline'}
        raise KeyboardInterrupt('unknown provider outcome')
    with pytest.raises(KeyboardInterrupt):
        evaluate(cases,'baseline','candidate',invoke=invoke,on_request=checkpoint)
    saved = json.loads(path.read_text())
    assert saved['provider_called'] is None and saved['observations'] == []
    assert saved['inflight']['arm'] == 'baseline'


def test_request_checkpoint_failure_prevents_provider_call():
    from evaluations.evolution.paired import evaluate
    cases = [{'id':str(i), 'objective':'Return true.', 'expected':True} for i in range(3)]
    def checkpoint(*args): raise OSError('cannot record intent')
    with pytest.raises(OSError):
        evaluate(cases,'baseline','candidate',
                 invoke=lambda *args, **kwargs: pytest.fail('must not dispatch'),
                 on_request=checkpoint)


def test_existing_report_blocks_live_rerun_before_provider_initialization(tmp_path, monkeypatch):
    from evaluations.evolution.paired import main
    cases = tmp_path/'cases.json'
    cases.write_text(json.dumps([{'id':str(i),'objective':'Return true.','expected':True} for i in range(3)]))
    baseline, candidate = tmp_path/'baseline', tmp_path/'candidate'
    baseline.write_text('baseline'); candidate.write_text('candidate')
    output = tmp_path/'report.json'
    original = '{"evaluation_state":"running","inflight":{"arm":"baseline"}}'
    output.write_text(original)
    monkeypatch.setattr('evaluations.evolution.chat_access.ChatEvaluator',
                        lambda **kwargs: pytest.fail('must not initialize client'))
    with pytest.raises(FileExistsError):
        main(['--cases',str(cases),'--baseline',str(baseline),'--candidate',str(candidate),
              '--output',str(output),'--owner','123','--live'])
    assert output.read_text() == original


def test_concurrent_report_admission_has_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from evaluations.evolution.paired import reserve_report
    output = tmp_path/'report.json'; barrier = Barrier(2)
    def claim(identity):
        barrier.wait(timeout=5)
        try:
            reserve_report(output, {'identity':identity})
            return identity
        except FileExistsError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [result for result in pool.map(claim, [1,2]) if result is not None]
    assert len(winners) == 1
    assert json.loads(output.read_text())['identity'] == winners[0]


def test_paired_python_compilation_rubric_keeps_cost_and_original_failures():
    from evaluations.evolution.paired import evaluate
    cases = [{'id':str(i),'objective':'Return JSON with python source.',
              'expected':{'compiles':True}} for i in range(3)]
    def invoke(messages, **kwargs):
        source = 'print(.dumps({}))' if messages[0]['content']=='baseline' else 'pass'
        return {'status':'done','content':json.dumps({'python':source}),'model':'fixed',
                'usage':{'input_tokens':10,'output_tokens':20}}
    result=evaluate(cases,'baseline','candidate',invoke=invoke,
                    checker='python_delivery_compile_v1')
    assert result['checker']=='python_delivery_compile_v1'
    assert result['comparison']['wins']==3
    assert result['costs']['baseline']['tokens']['total']==90
    assert 'print(.dumps' in result['observations'][0]['output_original']


@pytest.mark.parametrize('expected',[None,{'compiles':1},{'compiles':False},{'other':True}])
def test_invalid_compilation_rubric_refuses_before_model_call(expected):
    from evaluations.evolution.paired import evaluate
    cases=[{'id':str(i),'objective':'Return code.','expected':expected} for i in range(3)]
    with pytest.raises(ValueError,match='compilation cases'):
        evaluate(cases,'baseline','candidate',
                 invoke=lambda *args, **kwargs: pytest.fail('must not call'),
                 checker='python_delivery_compile_v1')


def test_retained_format_fidelity_cases_detect_string_corruption():
    from pathlib import Path
    from evaluations.evolution.paired import read_cases, exact_json
    path=Path(__file__).resolve().parents[2]/'evaluations/evolution/format_fidelity_cases.json'
    cases=read_cases(path)
    assert len(cases)==3
    for case in cases:
        original=json.dumps(case['expected'],ensure_ascii=False)
        assert exact_json(original,case['expected'])=='pass'
        assert exact_json(original.replace('json',''),case['expected'])=='fail'
        assert exact_json('```json\n'+original,case['expected'])=='fail'


@pytest.mark.parametrize('change', ['before_call','during_call','post_probe_failure'])
def test_chat_deployment_changes_preserve_cost_and_prevent_new_dispatch(change):
    from evaluations.evolution.chat_access import ChatEvaluator
    model={'creator_id':'test','model_id':'fixed'}
    state={'boot':'initial','calls':0,'health_failed':False}
    def request(path,body=None,**kwargs):
        if path.endswith('whoami'):return {'authenticated':True,'owner_id':123}
        if path.endswith('/models'):return {'models':[{'model':model}]}
        if path=='/api/health':
            if state['health_failed']:raise TimeoutError('probe failed')
            return {'bootId':state['boot'],'codeFingerprint':{'digest':None}}
        state['calls']+=1
        if change=='during_call':state['boot']='new'
        if change=='post_probe_failure':state['health_failed']=True
        return {'choices':[{'finish_reason':'stop','message':{'content':'true'}}],
                'usage':{'prompt_tokens':10,'completion_tokens':20},'task_id':'paid-call'}
    client=ChatEvaluator(user_id=123,request=request)
    assert client.deployment_identity['code_fingerprint']['digest'] is None
    if change=='before_call':
        state['boot']='new'
        with pytest.raises(RuntimeError,match='no new call'):client.invoke([],timeout_s=1)
        assert state['calls']==0
    else:
        result=client.invoke([],timeout_s=1)
        assert result['content']=='true' and result['usage']['completion_tokens']==20
        assert result['error']=='application_identity_unverified'
        assert result['deployment_identity_match'] is False
        with pytest.raises((RuntimeError,TimeoutError)):client.invoke([],timeout_s=1)
        assert state['calls']==1
