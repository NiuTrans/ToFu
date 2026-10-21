"""Canonical Artifact projection survives retention failure and mixed-round pressure.

Run the real pipeline; synthetic source handlers and retention replace external
effects, while canonical capture, budgeting and model delivery stay active.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.contracts import ToolContractV2
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for
from tests.tools.test_artifact_result_projection import REF, page, raw

pytestmark = pytest.mark.unit


def dispatch_sources(sources, mode):
    def handler(task, call, name, call_id, args, number, row, *rest):
        return call_id, sources[name], False

    task = task_for(handler, mode)
    parsed, rows = [], []
    for index, name in enumerate(sources):
        call_id = f'source-{index}'
        task['_toolContractDocumentsByName'][name] = ToolContractV2(
            name=name, parameters={'type': 'object', 'properties': {}},
            model_description='Read a synthetic owned source.').search_document()
        _, row, _ = _build_tool_round_entry(name, {}, call_id, '{}', index + 1, False)
        row['llmRound'] = 0
        rows.append(row)
        call = {'id': call_id, 'type': 'function',
                'function': {'name': name, 'arguments': '{}'}}
        parsed.append((call, name, call_id, {}, index + 1, row, None))
    task['toolRounds'] = rows
    messages, captured = [], {}
    execute_tool_pipeline(
        task, parsed, cfg=task['config'], project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=[], round_num=0,
        model='test-model', internal_result_sink=SimpleNamespace(
            capture=lambda call_id, value: captured.__setitem__(call_id, value)))
    return rows, messages, captured


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_retention_failure_stays_visible_and_preserves_live_native_evidence(
        mode, operation, observations, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError('injected retention failure')

    monkeypatch.setattr('lib.tools.result_value.persist_result', unavailable)
    value = page(operation, truncated=True, nextCursor='trq3.next.signature',
                 resumeCursor='trq3.original.signature')
    source = raw(value)
    rows, messages, captured = dispatch_sources({operation: source}, mode)

    assert captured['source-0'].text() == source
    assert captured['source-0'].execution_status == 'ok'
    assert rows[0]['status'] == 'done'
    assert rows[0]['toolResultEvidence']['retentionCoverage'] == 'unavailable'
    assert 'Canonical retention unavailable' in messages[0]['content']
    assert 'trq3.next.signature' in messages[0]['content']


@pytest.mark.parametrize('mode', ['legacy', 'v2'])
@pytest.mark.parametrize('pressure', [False, True])
def test_mixed_round_uses_retained_sources_for_aggregate_projection(
        mode, pressure, observations, monkeypatch):
    from lib.tasks_pkg.compaction import _budget

    retained = {}

    def persist(canonical, *, user_id, now_ms=None):
        reference = 'tool-result:' + format(len(retained) + 1000, '064x')
        retained[reference] = canonical.text()
        return reference

    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    monkeypatch.setattr('lib.tool_results.tool_result_artifacts.mint_artifact_alias',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text))
    if pressure:
        monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', 900)
    sources = {
        'read_tool_artifact': raw(page('read_tool_artifact', content='needle evidence ' * 150,
                                      resumeCursor='trq3.original.signature', provenance=[{
                                          'artifactRef': REF, 'path': [], 'segment': [],
                                          'start': 0, 'end': 2400}])),
        'fetch_url': 'Independent source evidence. ' * 100,
    }
    rows, messages, captured = dispatch_sources(sources, mode)
    assert [captured[f'source-{i}'].text() for i in range(2)] == list(sources.values())
    assert all(row['status'] == 'done' for row in rows)
    assert all(canonical.execution_status == 'ok' for canonical in captured.values())
    assert sources['read_tool_artifact'] in retained.values()
    assert len(messages) == 2
    assert all('"contractVersion"' not in item['content'] for item in messages)
    if pressure:
        assert sum(len(item['content']) for item in messages) <= 900
        assert sources['fetch_url'] in retained.values()
        for row in rows:
            evidence = row['toolResultEvidence']
            assert evidence['retentionCoverage'] == 'complete'
            assert evidence['artifactRef'] in retained
            assert evidence['truncated']
    else:
        assert 'needle evidence ' * 150 in messages[0]['content']
        assert messages[1]['content'] == sources['fetch_url']


def test_unrelated_legacy_source_documents_keep_their_original_representation(observations):
    source = raw(page('read_tool_artifact', provenance=[{
        'artifactRef': REF, 'path': [], 'segment': [], 'start': 0, 'end': 4}]))
    rows, messages, captured = dispatch_sources({'fetch_url': source}, 'legacy')
    assert json.loads(messages[0]['content']) == json.loads(source)
    assert captured['source-0'].text() == source
    assert rows[0]['status'] == 'done'
