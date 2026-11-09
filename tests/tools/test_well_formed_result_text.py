"""Tool and program results are well-formed Unicode before any consumer encodes them.

An unpaired UTF-16 surrogate has no UTF-8 encoding, so program retention,
artifact storage, the provider request and the browser all refuse it. Each
unpaired code unit becomes U+FFFD where the result value is formed; every
other character, including a pair split across two code points, survives.
"""
from __future__ import annotations

import json
import threading
import uuid

import pytest

from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.program_results import keep_program_value, list_program_inputs, read_program_result
from lib.tools.programmatic import ProgrammaticResultBudget
from lib.tools.result_value import CapturedToolResult, ResultPart, TrustedResultText, capture_result

pytestmark = pytest.mark.unit

# TB2.1 Tofu r1, constraints-scheduling: the prefetched web_search read the
# arXiv 2601.13155 PDF, whose pymupdf4llm table extraction clipped a
# Mathematical Alphanumeric Symbol to its high half, U+D835. Settlement crashed
# at offset 43471 of the result while sizing the program input.
_SEARCH_ARGUMENTS = {'query': 'unused probe - skip', 'vertical': 'off'}
_RECORDED_OFFSET = 43471
_EXTRACTED_TABLE = '|Col1|Col2|Col3|Col4|Col5|\n|---|---|---|---|---|\n|||||'


def _search_result(offset):
    head = '## Web results for "unused probe - skip"\n\n'
    line = '- arxiv.org/pdf/2601.13155: let 𝑿, 𝒀 ∈ ℝ^(𝑁×𝐷) be the block input and output.\n'
    filler = offset - len(head) - len(_EXTRACTED_TABLE)
    body = (line * (filler // len(line) + 1))[:filler]
    source = head + body + _EXTRACTED_TABLE + '\ud835' + '|\n\nwith 𝑁 the sequence length.\n'
    assert source.index('\ud835') == offset
    return source, source[:offset] + '\ufffd' + source[offset + 1:]


def _program_task():
    return {
        'id': 'well-formed-' + uuid.uuid4().hex, 'convId': 'cv-well-formed',
        'status': 'running', 'aborted': False, 'model': 'test-model', '_userId': 1,
        'events': [], 'events_lock': threading.Lock(), '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0, '_attended': False, '_ptc_local': {'tier': 'program'},
    }


def _strict_utf8_json(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


@pytest.fixture
def settle_prefetched_search(monkeypatch):
    """Settle one streaming-prefetched web_search through the real pipeline."""
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.executor import _finalize as executor_finalize
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from tests.tools.test_tool_settle_all_lanes import _mk_tc

    events, tasks = [], []
    for module in (pipeline, heartbeat, executor_finalize):
        monkeypatch.setattr(module, 'append_event',
                            lambda _task, event: events.append(dict(event)), raising=False)

    def settle(source):
        task = _program_task()
        tasks.append(task)
        task['_tool_result_cache'] = {
            _make_cache_key('web_search', _SEARCH_ARGUMENTS):
                (source, True, 'prefetch', [], None, None),
        }
        parsed = _mk_tc('call_38e38530f7b144ceb3ccdbcc', 'web_search', 1,
                        args=_SEARCH_ARGUMENTS)
        messages = []
        execute_tool_pipeline(
            task, [parsed], cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=messages,
            all_search_results_text=[], round_num=3, model='test-model')

        assert [message['role'] for message in messages] == ['tool']
        completions = [event for event in events if event.get('type') == 'tool_complete']
        assert len(completions) == 1
        # The provider request, the durable round and the browser event all need UTF-8.
        _strict_utf8_json(messages)
        _strict_utf8_json(completions)
        _strict_utf8_json(parsed[5]['toolContent'])
        return task, messages

    yield settle
    from lib.agent_core.admission import fire_terminal_callbacks
    for task in tasks:
        fire_terminal_callbacks(task['id'])


def test_recorded_prefetched_pdf_page_settles_in_program_tier(settle_prefetched_search):
    source, repaired = _search_result(_RECORDED_OFFSET)
    task, _messages = settle_prefetched_search(source)

    page = list_program_inputs(task, {'name': 'web_search'}, user_id=1,
                               catalog_names={'web_search'})
    assert page['total'] == 1
    assert page['inputs'][0]['bytes'] == len(repaired.encode('utf-8'))
    receipt = read_program_result(task, {'ref': page['inputs'][0]['ref']}, {}, user_id=1,
                                  budget=ProgrammaticResultBudget(),
                                  catalog_names={'web_search'})
    assert receipt['status'] == 'done'
    assert receipt.canonical.text() == repaired


def test_model_sees_the_unpaired_unit_as_a_replacement_character(settle_prefetched_search):
    source, repaired = _search_result(5000)
    _task, messages = settle_prefetched_search(source)
    assert str(messages[0]['content']) == repaired


@pytest.mark.parametrize('raw, expected', [
    ('a\ud835', 'a\ufffd'),
    ('\ud835x', '\ufffdx'),
    ('\udc7fy', '\ufffdy'),
    ('\udc7f\ud835', '\ufffd\ufffd'),
    ('\ud835\udc7f', '𝑿'),
    ('\ud835\ud835\udc7f', '\ufffd𝑿'),
    ('\ufeff𝑿 ∈ ℝ, 中文, café', '\ufeff𝑿 ∈ ℝ, 中文, café'),
    ('plain ascii', 'plain ascii'),
])
def test_unpaired_code_units_become_replacement_characters(raw, expected):
    from lib.tools.result_value import well_formed_text
    assert well_formed_text(raw) == expected


def test_result_values_cannot_hold_unencodable_text():
    page = 'page ' + _EXTRACTED_TABLE + '\ud835|'
    projection = TrustedResultText(page, canonical=CapturedToolResult((
        ResultPart('text', page, metadata=(('uri', 'file:///a\udc7f.pdf'), ('sourceOrdinal', 0))),)))
    expected = 'page ' + _EXTRACTED_TABLE + '\ufffd|'
    assert projection == expected
    assert projection.canonical.parts[0].text == expected
    assert projection.canonical.parts[0].metadata == (('uri', 'file:///a\ufffd.pdf'), ('sourceOrdinal', 0))
    structured = capture_result({'cell': '\ud835'})
    assert structured.parts[0].kind == 'json'
    assert json.loads(structured.text()) == {'cell': '\ufffd'}
    _strict_utf8_json(projection.canonical.manifest())


@pytest.fixture
def program():
    task = {'id': 'well-formed-program-' + uuid.uuid4().hex, '_ptc_local': {'tier': 'program'}}

    def run(code):
        return execute_javascript_program(
            code, search=lambda *a: [], call=lambda *a: None, call_many=lambda *a: None,
            value_keeper=lambda value: keep_program_value(
                task, value, user_id=7, catalog_names=set(), call_id='program'),
            result_reader=lambda selector, args: read_program_result(
                task, selector, args, user_id=7, catalog_names=set(),
                budget=ProgrammaticResultBudget()),
            input_lister=lambda query: list_program_inputs(
                task, query, user_id=7, catalog_names=set()))
    yield run
    from lib.agent_core.admission import fire_terminal_callbacks
    fire_terminal_callbacks(task['id'])


def test_program_values_with_split_strings_reach_the_host_well_formed(program):
    value, _ = program('const cut = "a𝑿".slice(0, 2); const row = {}; '
                       'row[cut] = [cut, "𝑿", "\\udc7f"]; return row;')
    assert value == {'a\ufffd': ['a\ufffd', '𝑿', '\ufffd']}


def test_program_keeps_and_rereads_a_value_with_a_split_string(program):
    saved, stats = program('return tools.keep({cell: "a𝑿".slice(0, 2)});')
    assert saved['kind'] == 'program_value' and stats['value_keeps'] == 1
    value, _ = program('return tools.result(' + json.dumps({'ref': saved['ref']}) + ');')
    assert value == {'cell': 'a\ufffd'}


def test_program_error_with_a_split_string_is_a_typed_failure(program):
    with pytest.raises(ProgramExecutionError) as caught:
        program('throw new Error("cut " + "a𝑿".slice(0, 2));')
    assert caught.value.code == 'javascript_error'
    assert 'cut a\ufffd' in str(caught.value)
