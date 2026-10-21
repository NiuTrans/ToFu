"""Final delivery budgets retain original evidence through real dispatch/storage."""
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tools import result_value
from tests.tasks_pkg.test_tool_outcome_finality import (
    dispatch, observations as observations, task_for,
)
from tests.tools.test_tool_result_query_backends import repo as repo, storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('outcome', ['error', 'program_input'])
@pytest.mark.parametrize('round_limit', [24000, 500])
def test_pipeline_counts_final_facts_and_recovers_original_after_restart(
        repo, storage, observations, monkeypatch, outcome, round_limit):
    limit = 1000
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', limit)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', limit)
    monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', round_limit)
    body = 'operation detail ' * (58 if outcome == 'error' else 800)
    executed, retained = [], []
    original_persist = result_value.persist_result

    def persist(value, **kwargs):
        retained.append(value.result_id)
        return original_persist(value, **kwargs)

    monkeypatch.setattr(result_value, 'persist_result', persist)

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        executed.append(call_id)
        if outcome == 'error':
            _finalize_tool_round(task, number, row, [{'content': body}], status='error')
        return call_id, body, False

    task = task_for(handler)
    if outcome == 'program_input':
        task['_ptc_local'] = {'tier': 'program'}
        task['_executable_tool_catalog'] = [{
            'type': 'function', 'function': {'name': 'fetch_url',
                                           'parameters': {'type': 'object'}}}]
    try:
        row, messages, captured = dispatch(task)
        text = messages[0]['content']
        evidence = row['toolResultEvidence']
        assert len(text.encode()) <= min(limit, round_limit)
        assert len(executed) == 1 and len(retained) == 1
        assert evidence['visibleBytes'] == len(text.encode())
        assert evidence['viewCoverage'] == 'partial'
        assert evidence['retentionCoverage'] == 'complete'
        assert captured[0].text() == body
        if outcome == 'error':
            assert evidence['status'] == evidence['executionStatus'] == 'error'
            assert 'rerun' not in text and 'programInput' not in json.loads(text)
        else:
            from lib.tools.program_results import read_program_result
            from lib.tools.programmatic import ProgrammaticResultBudget
            metadata = json.loads(text)['programInput']
            assert metadata['bytes'] == len(body.encode())
            assert metadata['scope'] == 'task_turn'
            # The real bank remains readable; presentation pressure grants no
            # additional tool invocation or ownership authority.
            original = read_program_result(task, {'ref': metadata['ref']}, {},
                user_id=1, budget=ProgrammaticResultBudget(), catalog_names={'fetch_url'})
            assert original['output'] == body and not original['output_truncated']
        storage.stop()
        storage.start()
        original = repo.read_range(user_id=1, artifact_ref=evidence['artifactRef'], limit=65536)
        assert original['content'] == body and original['nextCursor'] is None
        assert original['retentionCoverage'] == 'complete'
    finally:
        from lib.agent_core.admission import fire_terminal_callbacks
        fire_terminal_callbacks(task['id'])
