"""Bundled optional capabilities explain themselves in Tool Search.

produce_slides / paper_build / local_serve ship dormant behind the bundled
plugin allow-list, so a deployment without ``production`` in
``TOFU_DEFAULT_TOOL_PLUGINS`` used to return zero hits with no hint that the
capability exists. Evidence is read-only: it never enables the plugin, never
returns a dormant schema and never fires on an enabled plugin.
"""
from __future__ import annotations

import json

import pytest

from lib.tasks_pkg.handlers import tool_gateway
from lib.tasks_pkg.tool_search_projection import tool_search_display_results

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def deployment_without_production(monkeypatch):
    # Reproduce the observed deployment default exactly.
    monkeypatch.setenv('TOFU_DEFAULT_TOOL_PLUGINS', 'environments')


def _task(**extra):
    return {'id': 'bundled-task', '_userId': 7,
            '_executable_tool_catalog': [], '_executableToolNamespaceByName': {},
            **extra}


def _search(monkeypatch, task, query, namespace='', cfg=None):
    monkeypatch.setattr(tool_gateway, '_finalize', lambda *a, **kw: None)
    _, content, _ = tool_gateway.handle_search_tools(
        task, {}, 'search_tools', 'bundled-search',
        {'query': query, 'namespace': namespace}, 1, {}, cfg or {}, None, False)
    return json.loads(content)


def test_exact_name_of_a_dormant_bundled_tool_names_its_plugin(monkeypatch):
    result = _search(monkeypatch, _task(), 'produce_slides')
    assert result['items'] == [], 'a dormant schema is never disclosed'
    [evidence] = result['capability_availability']
    assert evidence['namespace'] == 'production'
    assert evidence['tool_name'] == 'produce_slides'
    assert evidence['available'] is False
    assert evidence['reason_code'] == 'bundled_plugin_not_enabled'
    assert 'TOFU_DEFAULT_TOOL_PLUGINS' in evidence['recovery_hint']
    assert 'config.plugins' in evidence['recovery_hint']
    assert 'missing_name' not in result
    assert 'TOFU_DEFAULT_TOOL_PLUGINS' in result['notice']
    ui = tool_search_display_results(result)
    assert ui[-1]['type'] == 'tool_catalog_availability'
    assert ui[-1]['reasonCode'] == 'bundled_plugin_not_enabled'


def test_natural_language_deck_request_also_finds_the_plugin(monkeypatch):
    result = _search(monkeypatch, _task(), '帮我做一份PPT 幻灯片')
    [evidence] = result['capability_availability']
    assert evidence['tool_name'] == 'produce_slides'
    assert 'produce_slides' in evidence['message']


def test_enabled_plugin_produces_no_unavailable_evidence(monkeypatch):
    result = _search(monkeypatch, _task(), 'produce_slides',
                     cfg={'plugins': ['production']})
    assert 'capability_availability' not in result


def test_wildcard_allow_list_produces_no_unavailable_evidence(monkeypatch):
    result = _search(monkeypatch, _task(), 'produce_slides',
                     cfg={'plugins': '*'})
    assert 'capability_availability' not in result


def test_deployment_default_enabling_production_produces_no_evidence(monkeypatch):
    monkeypatch.setenv('TOFU_DEFAULT_TOOL_PLUGINS', 'environments,production')
    assert 'capability_availability' not in _search(
        monkeypatch, _task(), 'produce_slides')


def test_image_switch_hint_keeps_the_documented_ui_gate(monkeypatch):
    result = _search(monkeypatch, _task(), 'generate_image')
    [evidence] = result['capability_availability']
    assert evidence['namespace'] == 'image_generation'
    assert '图片生成开关' in evidence['recovery_hint']


def test_plugin_namespace_filter_explains_the_whole_family(monkeypatch):
    result = _search(monkeypatch, _task(), 'anything', namespace='papers')
    [evidence] = result['capability_availability']
    assert evidence['namespace'] == 'papers'
    assert evidence['reason_code'] == 'bundled_plugin_not_enabled'


def test_no_evidence_for_unrelated_or_generic_queries(monkeypatch):
    for query in ('how do I write tests', 'terminal bench evaluation'):
        assert 'capability_availability' not in _search(
            monkeypatch, _task(), query)


def test_repeated_query_keeps_the_enable_guidance(monkeypatch):
    task = _task()
    _search(monkeypatch, task, 'produce_slides')
    repeated = _search(monkeypatch, task, 'produce_slides')
    assert repeated['repeated_query'] == 2
    assert 'TOFU_DEFAULT_TOOL_PLUGINS' in repeated['notice']


def test_programmatic_search_has_the_same_bundled_plugin_evidence(monkeypatch):
    monkeypatch.setattr(tool_gateway, '_finalize', lambda *a, **kw: None)
    task = {'id': 'bundled-program', '_userId': 7, 'model': 'test',
            '_executable_tool_catalog': []}
    _, content, _ = tool_gateway.handle_execute_tools(
        task, {}, 'execute_tools', 'bundled-program',
        {'program': 'return catalog.search("produce_slides");'},
        1, {'llmRound': 0}, {'tools': {'programmaticCalling': 'on'}}, None, False)
    result = json.loads(content)['program']['result']
    assert result['capability_availability'][0]['reason_code'] == (
        'bundled_plugin_not_enabled')
