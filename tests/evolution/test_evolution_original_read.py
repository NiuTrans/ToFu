"""Diagnostic archive reads preserve complete originals and exact source identity."""
import json
import pytest
from lib.evolution import stage_outcomes
from lib.evolution.contracts import EvolutionStateError
from tests.evolution.test_evolution_semantic_memory import runtime as runtime, archived, original_sources as original_sources
from tests.evolution.test_evolution_exploration_tools import call, OWNER

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['deliverable', 'tool_evidence'])
def test_read_original_pages_reach_tail_without_model_spending(runtime, original_sources, kind):
    program, task, attempts, receipt, content, metadata = original_sources
    tool_content = '工具前段观察。' * 4800 + 'TOOL_TAIL: network saturated'
    offset, digest, pieces = 0, None, []
    while True:
        page = call(task, program, 'read', sequence=1, source_kind=kind, offset=offset,
                    **({'source_digest': digest} if digest else {}))
        assert page['source_kind'] == kind and len(page['content']) <= 6000
        if digest:
            assert page['source_digest'] == digest
        digest = page['source_digest']
        pieces.append(page['content'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    complete = ''.join(pieces)
    assert (complete if kind == 'deliverable' else json.loads(complete)['content']) == (content if kind == 'deliverable' else tool_content)
    assert attempts == []


def test_wrong_original_digest_is_rejected_before_read(runtime, original_sources):
    program, task, attempts, *_ = original_sources
    with pytest.raises(EvolutionStateError, match='identity changed'):
        call(task, program, 'read', sequence=1, source_kind='tool_evidence', source_digest='0'*64)
    assert attempts == []


@pytest.mark.parametrize('mutation', ['missing', 'digest', 'length', 'task', 'part'])
def test_unavailable_or_changed_original_never_becomes_receipt_preview(runtime, original_sources, mutation):
    program, task, attempts, receipt, content, metadata = original_sources
    programs = runtime[0]
    receipt = programs.experiment_page(program['program_id'], user_id=OWNER, limit=1)['experiments'][0]
    if mutation == 'part':
        key = stage_outcomes._part_key(metadata['task_id'], 0, OWNER, 'tool_evidence')
        original = programs.records.get_record(stage_outcomes.PART_NAMESPACE, key)
        original['text'] = 'corrupt' + original['text'][7:]
        programs.records.put_record(stage_outcomes.PART_NAMESPACE, key, original, kind='part')
    else:
        reference = receipt['tool_evidence_source']
        if mutation == 'missing':
            receipt.pop('tool_evidence_source')
        elif mutation == 'digest':
            reference['source_sha256'] = '0'*64
        elif mutation == 'length':
            reference['total_chars'] += 1
        else:
            reference['task_id'] = 'foreign-original'
        programs.records.put_record(programs.EXPERIMENTS,
            programs._experiment_key(program['program_id'], 1, OWNER), receipt, kind='experiment')
    with pytest.raises(EvolutionStateError):
        call(task, program, 'read', sequence=1, source_kind='tool_evidence')
    assert attempts == []


@pytest.mark.parametrize('mutation', ['cancel', 'replace_executor', 'pause', 'stop'])
def test_original_delivery_rechecks_executor_after_storage_read(runtime, original_sources, monkeypatch, mutation):
    from lib.evolution import exploration_tools
    program, task, attempts, *_ = original_sources
    programs = runtime[0]
    read = exploration_tools._read
    def change_during_read(*args):
        result = read(*args)
        if mutation == 'stop':
            import threading
            task['abort_event'] = threading.Event()
            task['abort_event'].set()
        elif mutation == 'pause':
            programs.command_program(program['program_id'], {'action': 'pause'}, user_id=OWNER)
        else:
            run = programs.runs.get_run(program['current']['run_id'], user_id=OWNER)
            if mutation == 'cancel':
                run['status'] = 'cancelled'
            else:
                run['task_id'] = 'replacement-executor'
            programs.records.put_record(programs.records.NAMESPACE_RUNS,
                programs.records.entity_key(run['run_id'], OWNER, kind='run'), run, kind='run')
        return result
    monkeypatch.setattr(exploration_tools, '_read', change_during_read)
    if mutation == 'pause':
        result = call(task, program, 'read', sequence=1, source_kind='tool_evidence')
        assert result['source_kind'] == 'tool_evidence'
        assert programs.get_program(program['program_id'], user_id=OWNER)['status'] == 'paused'
    else:
        with pytest.raises(EvolutionStateError, match='stale or foreign|no tool authority'):
            call(task, program, 'read', sequence=1, source_kind='tool_evidence')
    assert attempts == []
