"""Backend-authoritative LLM canvas display contract (no external MCP calls)."""
import json

import pytest

from lib.tasks_pkg.handlers.mcp import _settle_mcp_round_display
from lib.tasks_pkg.tool_display._mcp import _mcp_links, compose_mcp_display
from lib.tasks_pkg.tool_display._renderers import _tool_display_mcp

BASE = 'https://model.internal.example.com/user/modelManage/experimentalTaskDetail/'
TOOL = 'mcp__llm__experiment_get'

pytestmark = pytest.mark.unit


def settle(args, result, tool=TOOL):
    display, _ = compose_mcp_display(tool, args, user_id=917)
    row, meta = {'query': display}, {}
    _settle_mcp_round_display(row, meta, tool, args, json.dumps(result), user_id=917)
    assert row['query'] == meta['title']
    for label in row.get('_mcpLinks', {}):
        assert label in row['query']
    return row


@pytest.mark.parametrize('key', ['experiment_id_or_name_or_url', 'experiment_or_url',
                                    'experiment_id_or_name', 'experiment_id'])
def test_numeric_start_and_failure_not_run_links(key):
    args = {key: '381153'}
    display, extra = _tool_display_mcp(TOOL, args, '', '', user_id=917)
    assert '381153' in display
    assert extra['_mcpLinks'] == {'381153': BASE + '381153'}
    for result in ({'ok': False, 'error': 'has not run yet'},
                   {'ok': True, 'data': {'experimentId': 381153, 'state': 'not_run',
                                        'canvasUrl': BASE + '381153'}}):
        assert settle(args, result)['_mcpLinks'] == extra['_mcpLinks']


def test_name_only_resolves_from_successful_top_level_experiment_id():
    args = {'experiment_id_or_name_or_url': 'canvas name'}
    assert not _mcp_links(args, fn_name=TOOL)
    assert settle(args, {'ok': True, 'data': {'experimentId': 381154}})['_mcpLinks'] == {
        'canvas name': BASE + '381154'}
    for result in ({'ok': False, 'data': {'experimentId': 381154}},
                   {'ok': True, 'data': {'id': 381154, 'taskId': 381154}},
                   {'ok': True, 'data': {'payload': {'experimentId': 381154}}},
                   {'ok': True, 'data': {'experimentId': True}}):
        assert '_mcpLinks' not in settle(args, result)


@pytest.mark.parametrize('tool', ['experiment_fork', 'experiment_data_fork'])
def test_fork_new_canvas_is_not_source(tool):
    name = 'mcp__llm__' + tool
    args = {'base_id': '381153', 'name': 'new canvas'}
    display, _ = compose_mcp_display(name, args)
    links = _mcp_links(args, fn_name=name)
    assert links == {'source: 381153': BASE + '381153'}
    assert all(label in display for label in links)
    row = settle(args, {'ok': True, 'data': {'experimentId': 381154,
                                          'experimentName': 'new canvas',
                                          'page': BASE + '381154'}}, name)
    assert row['_mcpLinks'] == {'source: 381153': BASE + '381153',
                               'new: new canvas': BASE + '381154'}
    args['base_id'] = 'source name'
    row = settle(args, {'ok': True, 'data': {'experimentId': 381154}}, name)
    assert 'source: source name' not in row['_mcpLinks']
    assert '_mcpLinks' not in settle(args, {'ok': False, 'error': 'denied'}, name)


@pytest.mark.parametrize('url', ['javascript:alert(1)', BASE + '381154\" onclick=\"bad',
                                BASE.replace('model.internal.example.com', 'evil.test') + '381154',
                                BASE + '381154?x=bad', BASE + '381155',
                                'https://model.internal.example.com@evil.test/x'])
def test_untrusted_result_urls_do_not_link_names(url):
    row = settle({'experiment_id': '<b>name</b>'},
                 {'ok': True, 'data': {'experimentId': 381154, 'canvasUrl': url}})
    assert '_mcpLinks' not in row


def test_valid_run_url_links_canvas_and_other_servers_do_not():
    args = {'experiment_or_url': BASE + '381153/991/0'}
    display, _ = compose_mcp_display(TOOL, args)
    links = _mcp_links(args, fn_name=TOOL)
    assert links == {'381153': BASE + '381153'}
    assert all(label in display for label in links)
    assert not _mcp_links({'experiment_id': 381153}, fn_name='mcp__other__experiment_get')
    assert not _mcp_links({'experiment_id': 381153}, fn_name='mcp__llm__eval_get')


def test_progressive_start_uses_underlying_server_and_args():
    display, extra = _tool_display_mcp('call_mcp_read_tool', {
        'name': TOOL, 'arguments': {'experiment_id': 381154}}, '', '', user_id=917)
    assert '381154' in display
    assert extra['_mcpLinks'] == {'381154': BASE + '381154'}


def test_capped_untrusted_label_is_exact_and_href_is_only_canonical():
    name = '<script>&"' + 'x' * 60
    row = settle({'experiment_id': name}, {'ok': True, 'data': {'experimentId': 381154}})
    label, href = next(iter(row['_mcpLinks'].items()))
    assert len(label) == 40 and label.endswith('…')
    assert label in row['query']
    assert href == BASE + '381154'  # Raw label stays data; browser escapes it.


def test_batch_numeric_ids_and_dry_run_source_only():
    args = {'ids': '381153,381154,381155'}
    display, _ = compose_mcp_display('mcp__llm__experiment_run', args)
    links = _mcp_links(args, fn_name='mcp__llm__experiment_run')
    assert links == {'381153': BASE + '381153', '381154': BASE + '381154'}
    assert '+1 more' in display
    row = settle({'base_id': '381153', 'name': 'copy', 'dry_run': True},
                 {'ok': True, 'data': {'dry_run': True, 'experimentId': 381154}},
                 'mcp__llm__experiment_fork')
    assert row['_mcpLinks'] == {'source: 381153': BASE + '381153'}


def test_settle_removes_stale_name_link_after_failure():
    row = {'query': '', '_mcpLinks': {'unresolved': BASE + '381153'}}
    _settle_mcp_round_display(row, {}, TOOL, {'experiment_id': 'unresolved'},
                              '{"ok":false,"error":"denied"}', user_id=917)
    assert '_mcpLinks' not in row


WRAPPED_DATA = {'experimentId': 381491, 'experimentName': 'Fork canvas',
                'page': BASE + '381491'}
WRAPPED_ITEM = {'ok': True, 'data': WRAPPED_DATA}
WRAPPED_FORK = {'items': [WRAPPED_ITEM], 'summary': 'Tool returned a structured result.'}


def test_historical_single_item_fork_wrapper():
    tool = 'mcp__llm__experiment_fork'
    args = {'base_id': 379851, 'dry_run': False}
    expected = {'source: 379851': BASE + '379851', 'new: Fork canvas': BASE + '381491'}
    for content in (WRAPPED_FORK, json.dumps(WRAPPED_FORK)):
        assert _mcp_links(args, fn_name=tool, tool_content=content) == expected
    assert settle(args, WRAPPED_FORK, tool)['_mcpLinks'] == expected
    assert settle({**args, 'dry_run': True}, WRAPPED_FORK, tool)['_mcpLinks'] == {
        'source: 379851': BASE + '379851'}


def hostile_wrappers():
    yield {**WRAPPED_FORK, 'items': []}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_ITEM, WRAPPED_ITEM]}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_ITEM, {'ok': False}]}
    yield {**WRAPPED_FORK, 'summary': 'Tool returned 1 items.'}
    yield {'items': [WRAPPED_ITEM]}
    yield {**WRAPPED_FORK, 'items': {'0': WRAPPED_ITEM, 'length': 1}}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_FORK]}
    yield {**WRAPPED_FORK, 'items': [json.dumps(WRAPPED_ITEM)]}
    yield {**WRAPPED_FORK, 'items': [WRAPPED_DATA]}
    yield {**WRAPPED_FORK, 'items': [{'ok': True, 'data': {**WRAPPED_DATA, 'page': BASE + '381492'}}]}
    for extra in ({'ok': False}, {'success': False}, {'error': 'failed'}, {'isError': True},
                  {'status': 'cancelled'}, {'status': 'partial'}, {'truncated': True}, {'dry_run': True}):
        yield {**WRAPPED_FORK, **extra}
        yield {**WRAPPED_FORK, 'items': [{**WRAPPED_ITEM, **extra}]}
        yield {**WRAPPED_FORK, 'items': [{'ok': True, 'data': {**WRAPPED_DATA, **extra}}]}
    yield {**WRAPPED_FORK, 'artifactRef': 'tool-result:partial'}
    yield json.dumps(WRAPPED_FORK)[:-1]
    yield json.dumps(WRAPPED_FORK) + ' truncated'
    yield ' ' * 65537 + json.dumps(WRAPPED_FORK)


@pytest.mark.parametrize('wrapper', list(hostile_wrappers()))
def test_hostile_wrapper_keeps_only_source(wrapper):
    tool = 'mcp__llm__experiment_fork'
    for content in (wrapper, json.dumps(wrapper) if not isinstance(wrapper, str) else wrapper):
        assert _mcp_links({'base_id': 379851}, fn_name=tool, tool_content=content) == {
            'source: 379851': BASE + '379851'}


def test_original_retained_source_requires_owning_complete_evidence():
    from pathlib import Path
    original = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' /
                           'llm_canvas_original_fork.json').read_text())
    tool, args = original['toolName'], original['toolArgs']
    evidence, content = original['toolResultEvidence'], original['toolContent']
    source = {'source: 379851': BASE + '379851'}
    for value in (content, json.dumps(content)):
        links = _mcp_links(args, fn_name=tool, tool_content=value, tool_result_evidence=evidence)
        assert list(links.values()) == [BASE + '379851', BASE + '381491']
        title, _ = compose_mcp_display(tool, args, tool_content=value, tool_result_evidence=evidence)
        assert all(label in title for label in links)
        assert _mcp_links(args, fn_name=tool, tool_content=value) == source
    hostile = [None, {}, *[{**evidence, key: None} for key in (
        'contractVersion', 'resultContractVersion', 'status', 'executionStatus',
        'projectionKind', 'retentionCoverage', 'viewCoverage', 'truncated')]]
    hostile += [{**evidence, **extra} for extra in (
        {'status': 'error'}, {'status': 'failed'}, {'status': 'cancelled'}, {'status': 'partial'},
        {'executionStatus': 'failed'}, {'retentionCoverage': 'unknown'}, {'retentionCoverage': 'partial'},
        {'viewCoverage': 'unknown'}, {'viewCoverage': 'partial'}, {'sourceCoverage': 'partial'},
        {'truncated': True}, {'truncated': 0}, {'artifactRef': ''}, {'resultId': ''}, {'evidenceId': ''},
        {'isError': True}, {'error': 'bad'}, {'success': False}, {'ok': False}, {'dry_run': True})]
    for witness in hostile:
        assert _mcp_links(args, fn_name=tool, tool_content=content, tool_result_evidence=witness) == source
    assert _mcp_links({**args, 'dry_run': True}, fn_name=tool, tool_content=content,
                      tool_result_evidence=evidence) == source
    for wrapper in hostile_wrappers():
        if isinstance(wrapper, dict) and wrapper.get('summary') == WRAPPED_FORK['summary']:
            wrapper = {**wrapper, 'summary': content['summary']}
        assert _mcp_links(args, fn_name=tool, tool_content=wrapper, tool_result_evidence=evidence) == source


def test_retained_annotation_uses_trusted_delivery_sidecar_not_producer_envelope():
    from pathlib import Path
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tools.result_value import TrustedResultText
    original = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' /
                           'llm_canvas_original_fork.json').read_text())
    evidence = original['toolResultEvidence']
    envelope = {**original['toolContent'], **evidence,
                'contractVersion': evidence['resultContractVersion']}
    text = json.dumps(envelope)
    delivery = split_tool_result_delivery(TrustedResultText(text, envelope=True))
    assert delivery.evidence['projectionKind'] == 'summary_items'
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert delivery.evidence['sourceCoverage'] == 'unknown'
    links = _mcp_links(original['toolArgs'], fn_name=original['toolName'],
                       tool_content=delivery.model_text, tool_result_evidence=delivery.evidence)
    assert list(links.values()) == [BASE + '379851', BASE + '381491']
    # The same producer-controlled JSON cannot manufacture an owning sidecar.
    assert split_tool_result_delivery(text).evidence is None
    assert _mcp_links(original['toolArgs'], fn_name=original['toolName'], tool_content=text) == {
        'source: 379851': BASE + '379851'}
