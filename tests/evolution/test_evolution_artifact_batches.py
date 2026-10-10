"""Complete multi-file handoff with partial failures and unchanged source authority."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.evolution import artifact_evidence as evidence
from lib.evolution.contracts import EvolutionContractError, EvolutionStateError
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_artifact_evidence import (
    project_run, read_artifacts, runtime as task_runtime, no_reindex as inherited_no_reindex,
)
from tests.evolution.test_evolution_runs import finish, OWNER

pytestmark = pytest.mark.unit
runtime = task_runtime
no_reindex = inherited_no_reindex


def files(project, count=5):
    bodies = {f'result-{i}.json': f'complete {i} 🧪\n' * (i + 1) for i in range(count)}
    for path, body in bodies.items():
        (project / path).write_text(body)
    return bodies


def test_complete_set_one_call_retains_each_body_after_live_file_loss(runtime, tmp_path):
    run, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path)
    batch = evidence.declare(executor, {'paths': list(bodies)})
    assert batch['complete'] and batch['atomic'] is False
    assert batch['run_id'] == run['run_id'] and batch['task_id'] == executor['id']
    assert [r['path'] for r in batch['results']] == list(bodies)
    for row in batch['results']:
        assert row['snapshot']['state'] == 'sealed'
        assert row['snapshot']['content_sha256'] == hashlib.sha256(bodies[row['path']].encode()).hexdigest()
        (tmp_path / row['path']).unlink()
    reviewing = finish(runtime, executor, 'Inspect the five complete declared artifacts.')
    rows = [json.loads(line) for line in read_artifacts(runtime[2][-1], reviewing).splitlines()]
    assert {row['relative_path']: row['content'] for row in rows} == bodies
    assert reviewing['artifact_evidence_source']['complete']


@pytest.mark.parametrize('args', [
    {}, {'path': 'x', 'paths': ['x']}, {'paths': []}, {'paths': ['x', 'x']},
    {'paths': [str(i) for i in range(17)]}, {'paths': 'x'}, {'paths': [3]},
    {'paths': ['x'], 'extra': True},
])
def test_invalid_batch_shape_refuses_before_any_capture(runtime, tmp_path, args):
    _, executor = project_run(runtime, tmp_path)
    (tmp_path / 'x').write_text('value')
    before = runtime[1].writes
    with pytest.raises(EvolutionContractError):
        evidence.declare(executor, args)
    assert runtime[1].writes == before


def test_sibling_refusal_is_explicit_and_successes_remain_idempotent(runtime, tmp_path):
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 2)
    requested = ['result-0.json', '../escape', 'missing.json', 'result-1.json']
    result = evidence.declare(executor, {'paths': requested})
    assert not result['complete'] and result['atomic'] is False
    assert [r['path'] for r in result['results']] == requested
    assert all('snapshot' in result['results'][i] for i in (0, 3))
    assert all('error' in result['results'][i] for i in (1, 2))
    before = runtime[1].writes
    repeated = evidence.declare(executor, {'paths': list(bodies)})
    assert repeated['complete'] and runtime[1].writes == before
    assert [r['snapshot'] for r in repeated['results']] == [result['results'][i]['snapshot'] for i in (0, 3)]


def test_reserved_storage_failure_preserves_other_bodies_and_unknown_source(runtime, tmp_path, monkeypatch):
    from lib.evolution import stage_outcomes
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 3)
    put = stage_outcomes._put_once
    failed = []
    def interrupted(namespace, key, part, kind):
        if namespace == evidence.PARTS and 'complete 1' in part['text']:
            failed.append(key)
            raise StorageError('unavailable', 'part store interrupted')
        return put(namespace, key, part, kind)
    with monkeypatch.context() as scoped:
        scoped.setattr(stage_outcomes, '_put_once', interrupted)
        batch = evidence.declare(executor, {'paths': list(bodies)})
    assert failed and not batch['complete'] and 'error' in batch['results'][1]
    reviewing = finish(runtime, executor, 'Capture incomplete; preserve the missing-body check.')
    assert reviewing['actuals']['tokens'] == 15
    assert not reviewing['artifact_evidence_source']['complete']
    rows = [json.loads(line) for line in read_artifacts(runtime[2][-1], reviewing).splitlines()]
    assert rows[0]['content'] == bodies['result-0.json']
    assert rows[1]['state'] == 'unavailable' and rows[1]['content'] is None
    assert rows[2]['content'] == bodies['result-2.json']


def test_partial_batch_retry_seals_same_reservation_without_extra_generation(runtime, tmp_path, monkeypatch):
    from lib.evolution import stage_outcomes
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 2)
    put = stage_outcomes._put_once
    def interrupted(namespace, *args):
        if namespace == evidence.PARTS:
            raise StorageError('unavailable', 'temporary store loss')
        return put(namespace, *args)
    with monkeypatch.context() as scoped:
        scoped.setattr(stage_outcomes, '_put_once', interrupted)
        failed = evidence.declare(executor, {'paths': list(bodies)})
    assert not failed['complete'] and all('error' in r for r in failed['results'])
    sealed = evidence.declare(executor, {'paths': list(bodies)})
    assert sealed['complete']
    run = finish(runtime, executor, 'Both original reservations sealed.')
    rows = [json.loads(line) for line in read_artifacts(runtime[2][-1], run).splitlines()]
    assert len(rows) == 2 and all(r['state'] == 'sealed' for r in rows)


def test_generation_capacity_partial_response_cannot_discard_or_expand_old_set(runtime, tmp_path):
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, evidence.MAX_FILES + 1)
    initial = evidence.declare(executor, {'paths': list(bodies)[:15]})
    batch = evidence.declare(executor, {'paths': ['result-15.json', 'result-16.json', 'result-0.json']})
    assert initial['complete'] and not batch['complete']
    assert 'snapshot' in batch['results'][0] and 'error' in batch['results'][1]
    assert batch['results'][2]['snapshot'] == initial['results'][0]['snapshot']
    run = finish(runtime, executor, 'Seventeen requested; one capacity refusal remains explicit.')
    rows = [json.loads(line) for line in read_artifacts(runtime[2][-1], run).splitlines()]
    assert len(rows) == evidence.MAX_FILES
    assert 'result-16.json' not in {r['relative_path'] for r in rows}


def test_authority_loss_mid_batch_never_reads_later_paths(runtime, tmp_path, monkeypatch):
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 3)
    read = evidence._read_file
    seen = []
    def close_after_first(project, path):
        seen.append(path)
        raw = read(project, path)
        executor['status'] = 'aborted'
        return raw
    monkeypatch.setattr(evidence, '_read_file', close_after_first)
    batch = evidence.declare(executor, {'paths': list(bodies)})
    assert not batch['complete'] and seen == ['result-0.json']
    assert all('error' in r for r in batch['results'])


def test_reviewer_foreign_owner_and_terminal_executor_refuse_batch(runtime, tmp_path):
    run, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 1)
    evidence.declare(executor, {'paths': list(bodies)})
    finish(runtime, executor, 'Captured before terminal closure.')
    reviewer = runtime[2][-1]
    for task in [reviewer, executor, {**executor, '_userId': OWNER + 1}]:
        with pytest.raises(EvolutionStateError):
            evidence.declare(task, {'paths': list(bodies)})


def test_concurrent_batch_and_scalar_share_one_generation_bound(runtime, tmp_path):
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        batch = pool.submit(evidence.declare, executor, {'paths': list(bodies)[:3]})
        single = pool.submit(evidence.declare, executor, {'path': 'result-3.json'})
        assert batch.result()['complete'] and single.result()['state'] == 'sealed'
    run = finish(runtime, executor, 'Both clients used the same manifest.')
    rows = [json.loads(line) for line in read_artifacts(runtime[2][-1], run).splitlines()]
    assert {r['relative_path']: r['content'] for r in rows} == bodies


def test_handler_exposes_partial_result_without_success_label(runtime, tmp_path):
    _, executor = project_run(runtime, tmp_path)
    files(tmp_path, 1)
    result = evidence.handle(executor, {}, 'evolution_artifact', 'capture',
        {'paths': ['result-0.json', 'missing']}, 1, {}, executor['config'], str(tmp_path), True)
    assert result[0]['fetched'] is False and result[0]['title'] == 'artifact capture partial'
    value = json.loads(result[0]['snippet'])
    assert not value['complete'] and 'snapshot' in value['results'][0] and 'error' in value['results'][1]


@pytest.mark.parametrize('cause', ['aborted', 'abort_event', 'deadline'])
def test_stop_or_deadline_mid_capture_fences_later_files(runtime, tmp_path, monkeypatch, cause):
    import threading
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 3)
    executor['abort_event'] = threading.Event()
    original = evidence._read_file
    seen = []
    def interrupt(project, path):
        seen.append(path)
        raw = original(project, path)
        if cause == 'aborted':
            executor['aborted'] = True
        elif cause == 'abort_event':
            executor['abort_event'].set()
        else:
            executor['config']['executionLimits']['deadline'] = 1
        return raw
    monkeypatch.setattr(evidence, '_read_file', interrupt)
    result = evidence.declare(executor, {'paths': list(bodies)})
    assert not result['complete'] and seen == ['result-0.json']
    assert all('error' in r for r in result['results'])


def test_mixed_text_binary_and_empty_files_keep_exact_selected_bytes(runtime, tmp_path):
    import base64
    _, executor = project_run(runtime, tmp_path)
    selected = {'text.txt': '完整正文 🧪'.encode(), 'binary.dat': b'\xff\x00\x80', 'empty.txt': b''}
    for name, body in selected.items():
        (tmp_path / name).write_bytes(body)
    result = evidence.declare(executor, {'paths': list(selected)})
    assert result['complete']
    run = finish(runtime, executor, 'Exact bytes saved; binary interpretation unverified.')
    rows = [json.loads(line) for line in read_artifacts(runtime[2][-1], run).splitlines()]
    restored = {r['relative_path']: r['content'].encode() if r['encoding'] == 'utf-8'
                else base64.b64decode(r['content']) for r in rows}
    assert restored == selected


def test_stop_between_parts_retains_reservation_without_writing_remaining_parts(runtime, tmp_path, monkeypatch):
    import threading
    from lib.evolution import stage_outcomes
    run, executor = project_run(runtime, tmp_path)
    executor['abort_event'] = threading.Event()
    (tmp_path / 'large.txt').write_text('complete 🧪' * 20000)
    original = stage_outcomes._put_once
    seen = []
    def stop_after_first(namespace, *args):
        result = original(namespace, *args)
        if namespace == evidence.PARTS:
            seen.append(args[0])
            executor['abort_event'].set()
        return result
    monkeypatch.setattr(stage_outcomes, '_put_once', stop_after_first)
    response = evidence.declare(executor, {'paths': ['large.txt']})
    assert not response['complete'] and len(seen) == 1
    frozen = evidence.freeze(run, executor, user_id=OWNER)
    rows = [json.loads(line) for line in evidence.lines(frozen, OWNER)]
    assert len(rows) == 1 and rows[0]['state'] == 'unavailable' and rows[0]['content'] is None


def test_exposed_tool_schema_is_self_contained_and_cannot_mutate_contract():
    from types import SimpleNamespace
    from jsonschema import Draft202012Validator
    from lib.evolution.contracts import load_schema
    ctx = SimpleNamespace(cfg={'evolution': {'run_id': 'owned', 'stage': 'executing',
        'project_path': '/project'}, 'projectEnabled': True}, project_enabled=True)
    schema = evidence.build(ctx)[0]['function']['parameters']
    Draft202012Validator(schema).validate({'paths': ['a', 'b']})
    Draft202012Validator(schema).validate({'path': 'a'})
    assert 'oneOf' not in schema
    schema['properties']['paths']['maxItems'] = 999
    assert load_schema('evolution_run_v1.schema.json')['$defs']['artifact_declare_request']['properties']['paths']['maxItems'] == 16


def test_last_admitted_tool_slot_captures_whole_batch_without_extra_slots(runtime, tmp_path):
    from lib.agent_core.execution_budget import model_budget_admission
    from lib.agent_core.execution_limits import admit_tool_execution
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, evidence.MAX_FILES)
    executor['config']['executionLimits']['max_tool_calls'] = 1
    model_budget_admission(executor)
    assert admit_tool_execution(executor) is None
    assert evidence.declare(executor, {'paths': list(bodies)})['complete']
    assert executor['_executionBudget'].consumption_snapshot() == (0, 1, True)
    assert admit_tool_execution(executor) == 'execution_tool_budget_exhausted'
    assert executor['_executionBudget'].consumption_snapshot() == (0, 1, True)


def test_shared_account_unknown_usage_mid_batch_fences_later_reads(runtime, tmp_path, monkeypatch):
    from lib.agent_core.execution_budget import model_budget_admission
    _, executor = project_run(runtime, tmp_path)
    bodies = files(tmp_path, 2)
    executor['config']['executionLimits']['max_tokens'] = 100
    admission = model_budget_admission(executor)
    lease = admission(input_tokens=1, max_output_tokens=1)
    lease.mark_dispatched()
    original = evidence._read_file
    seen = []
    def lose_usage(project, path):
        seen.append(path)
        raw = original(project, path)
        lease.settle(None)
        return raw
    monkeypatch.setattr(evidence, '_read_file', lose_usage)
    response = evidence.declare(executor, {'paths': list(bodies)})
    assert not response['complete'] and seen == ['result-0.json']
    assert all(r['error'] == 'execution_token_usage_unknown' for r in response['results'])
    assert executor['_executionBudget'].actuals_complete() is False
