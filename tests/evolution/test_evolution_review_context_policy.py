"""Source review keeps cold originals until ordinary context pressure requires compaction.

Real stage births, immutable page reader and L1 pipeline; no external model.
"""
from copy import deepcopy
import json

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime, finish, OWNER
from tests.evolution.test_evolution_source_tools import large_review
from tests.evolution.test_evolution_artifact_evidence import artifact_review

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_external_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kw: [])


def source_messages(task, run, source):
    from lib.evolution.source_tools import handle
    from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched
    from lib.agent_core.execution_budget import model_budget_admission
    from types import SimpleNamespace
    messages = [{'role': 'user', 'content': 'Check the complete original evidence.'}]
    # Replay reads in the working context, as observed in the actual reviewer.
    # Each page still comes from the real immutable source/authorization owner.
    for pass_index in range(8):
        offset = 0
        while True:
            call_id = f'page-{pass_index}-{offset}'
            args = {'action': 'read', 'run_id': run['run_id'],
                    'task_id': source['task_id'],
                    'source_sha256': source['source_sha256'],
                    'source_kind': source.get('source_kind', 'deliverable'),
                    'offset': offset}
            entries = handle(task, None, 'evolution_source', call_id, args,
                             1, {}, task['config'], '', False)
            assert entries[0]['fetched']
            body = entries[0]['title'] + ': ' + entries[0]['snippet']
            messages.extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': call_id, 'type': 'function', 'function': {
                        'name': 'evolution_source', 'arguments': json.dumps(args)}}]},
                {'role': 'tool', 'tool_call_id': call_id, 'content': body}])
            # Each successful model input consumes this page's pending receipt;
            # the following rounds retain its original request-local text.
            with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(task)}) as scope:
                lease = scope.body['_model_budget_admit'](input_tokens=2000, max_output_tokens=1)
                mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease,
                    body={'messages': [messages[-1]]}))
                scope.usage = {'input_tokens': 2000, 'output_tokens': 1}
            page = json.loads(entries[0]['snippet'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
    messages.append({'role': 'assistant', 'content': 'Now assess every requirement.'})
    return messages


@pytest.mark.parametrize('kind', ['deliverable', 'tool_evidence', 'artifact_evidence'])
def test_review_pipeline_preserves_cold_original_pages(runtime, tmp_path, monkeypatch, kind):
    from lib.tasks_pkg.compaction import _pipeline
    if kind == 'deliverable':
        run, _, task = large_review(runtime)
        source = run['result_source']
    elif kind == 'artifact_evidence':
        run, _, task, _, _ = artifact_review(runtime, tmp_path)
        source = run['artifact_evidence_source']
    else:
        runtime[0].submit_run({'objective': 'Inspect the observed original tool results.'}, user_id=OWNER)
        executor = runtime[2][-1]
        executor['messages'] = [{'role': 'tool', 'tool_call_id': 'observation',
                                'content': 'Observed original result 🧪e\u0301\n' * 3000}]
        run = finish(runtime, executor, 'Inspect the complete original tool evidence.')
        task = runtime[2][-1]
        source = run['tool_evidence_source']
    messages = source_messages(task, run, {**source, 'source_kind': kind})
    before = deepcopy(messages)
    from lib.evolution.source_tools import fully_read
    assert fully_read(task, source)
    coverage_before = deepcopy({k: v for k, v in task.items() if k.startswith('_evolution') and k.endswith('Coverage')})
    assert len([m for m in messages if m['role'] == 'tool']) > 40
    pressure_checks = []
    # Isolate cheap L1 policy from capacity-triggered L2 summarization. The
    # production pressure/admission owner must still be called, not disabled.
    def pressure_boundary(msgs, **kwargs):
        pressure_checks.append(kwargs)
        return False
    monkeypatch.setattr(_pipeline, 'force_compact_if_needed', pressure_boundary)
    _pipeline.run_compaction_pipeline(messages, 60, task)
    assert messages == before
    assert len(pressure_checks) == 1
    assert pressure_checks[0]['task'] is task
    # L1 text retention leaves the independently observed access ledger alone.
    assert {k: v for k, v in task.items() if k.startswith('_evolution') and k.endswith('Coverage')} == coverage_before


def test_executor_keeps_default_cold_tool_compaction(runtime):
    from lib.tasks_pkg.compaction._layer1 import micro_compact
    runtime[0].submit_run({'objective': 'Analyze the observations.'}, user_id=OWNER)
    task = runtime[2][-1]
    assert 'compaction' not in task['config']
    messages = []
    for i in range(45):
        messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': str(i), 'function': {'name': 'run_command', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': str(i), 'content': 'Observation. ' * 600}])
    messages.append({'role': 'assistant', 'content': 'Proceed.'})
    before = deepcopy(messages)
    assert micro_compact(messages, task=task) > 0
    assert messages != before
    assert '[tool result compacted' in messages[1]['content']
