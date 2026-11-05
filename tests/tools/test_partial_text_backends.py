"""Partial text previews fit actual dispatch budgets and retain recoverable sources."""
import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import split_tool_result_delivery, tool_result_error, tool_result_observation
from lib.tools.result_projection_text import load_text_field
from tests.tasks_pkg.test_tool_outcome_finality import dispatch, observations as observations, task_for
from tests.tools.test_tool_result_query_backends import repo as repo, storage as storage

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('program', [False, True])
@pytest.mark.parametrize('round_limit', [4000, 900])
def test_partial_text_keeps_budget_program_source_and_restart_recovery(
        repo, storage, observations, monkeypatch, program, round_limit):
    limit = 1200
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', limit)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', limit)
    monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', round_limit)
    body = 'entry = {"path": "C:\\source\\file", "quoted": "value", "代码": "🙂"}\r\n' * 240
    calls = []

    def handler(task, tc, name, call_id, *args):
        calls.append(call_id)
        return call_id, body, False

    task = task_for(handler)
    if program:
        task['_ptc_local'] = {'tier': 'program'}
        task['_executable_tool_catalog'] = [{'type': 'function', 'function': {
            'name': 'fetch_url', 'parameters': {'type': 'object'}}}]
    try:
        row, messages, captured = dispatch(task)
        text, evidence = messages[0]['content'], row['toolResultEvidence']
        view = load_text_field(text, 'summary')
        assert view is not None and '"quoted": "value"' in view['summary']
        assert evidence['cursor'] == '0', 'Canonical manifests use their own cursor domain'
        assert len(text.encode()) <= min(limit, round_limit)
        assert calls == ['occurrence-1'] and captured[0].text() == body
        assert row['status'] == 'done' and captured[0].execution_status == 'ok'
        assert evidence['visibleBytes'] == len(text.encode())
        assert evidence['viewCoverage'] == 'partial' and evidence['retentionCoverage'] == 'complete'
        assert tool_result_observation(text, evidence)['summary'] == view['summary']
        assert tool_result_error(text) is None
        if program:
            from lib.tools.program_results import read_program_result
            from lib.tools.programmatic import ProgrammaticResultBudget
            metadata = view['programInput']
            assert metadata['bytes'] == len(body.encode())
            original = read_program_result(task, {'ref': metadata['ref']}, {},
                user_id=1, budget=ProgrammaticResultBudget(), catalog_names={'fetch_url'})
            assert original['output'] == body and not original['output_truncated']
        storage.stop()
        storage.start()
        original = repo.read_range(user_id=1, artifact_ref=evidence['artifactRef'],
                                   offset=evidence['cursor'], limit=65536)
        assert original['content'] == body and original['nextCursor'] is None
        assert original['retentionCoverage'] == 'complete'
        from lib.agent_core.storage_errors import StorageError
        try:
            denied = repo.read_range(user_id=2, artifact_ref=evidence['artifactRef'])
        except StorageError as error:
            assert error.code == 'database_forbidden'
        else:
            assert denied is None
        assert not task.get('_activeToolContexts')
    finally:
        from lib.agent_core.admission import fire_terminal_callbacks
        fire_terminal_callbacks(task['id'])


def test_round_raw_text_preview_resumes_exactly_after_visible_utf8_bytes(repo, storage, monkeypatch):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    body = 'Acquired "源码/🙂" = 9007199254740993\n' * 240
    source = _budget.enforce_round_aggregate_budget_v2(
        {'read': (body, 'fixture_read', 'read')}, user_id=1, token_budget=900)['read'][0]
    delivery = split_tool_result_delivery(source)
    view = tool_result_observation(delivery.model_text, delivery.evidence)
    offset = int(view['cursor'])
    assert 0 < offset < len(body.encode())
    shown = body.encode()[:offset].decode()
    assert view['summary'].startswith(shown)
    assert view['summary'][len(shown):].startswith('\n… [truncated;')
    assert len(delivery.model_text.encode()) <= 900
    storage.stop()
    storage.start()
    remainder = repo.read_range(user_id=1, artifact_ref=view['artifactRef'], offset=offset, limit=65536)
    assert shown + remainder['content'] == body
    assert remainder['nextCursor'] is None
