"""Artifact read views preserve exact text and recovery while avoiding JSON escaping."""
import json
import re
from types import SimpleNamespace

import pytest

from lib.tools.artifact_result_projection import project_artifact_result
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tools.result_envelope import split_tool_result_delivery
from tests.tools.test_artifact_result_projection import page, raw, restore_defaults

pytestmark = pytest.mark.unit

BODY = ('const item = {"quote": "中🙂", "path": "C:\\source\\file"};\r\n'
        '\tprint("exact evidence")\n') * 40


def decode_read_view(view):
    """Independent consumer of the declared JSON-or-length-labelled model format."""
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(view)
    if end == len(view):
        return value

    def page_tail(metadata, offset):
        label = re.match(r'\ncontent \(text; ([0-9]+) chars\):\n', view[offset:])
        if label is None:
            return metadata, offset
        start = offset + label.end()
        end = start + int(label.group(1))
        assert end <= len(view), 'body length exceeds delivered source'
        return {**metadata, 'content': view[start:end]}, end

    if view[end:].startswith('\ncontent '):
        value, end = page_tail(value, end)
    else:
        pages = []
        while end < len(view):
            heading = f'\n\n## items[{len(pages)}]\n'
            assert view.startswith(heading, end)
            item, end = decoder.raw_decode(view, end + len(heading))
            item, end = page_tail(item, end)
            pages.append(item)
        value['items'] = pages
    assert end == len(view), 'unattributed trailing bytes'
    return value


@pytest.mark.parametrize('more', [False, True])
def test_code_body_reaches_the_model_verbatim_with_all_page_evidence(more, monkeypatch):
    value = page('read_tool_artifact', content=BODY, offset=9007199254740993,
                 resumeCursor='trq3.exact-start.signature',
                 nextCursor='trq3.exact-next.signature' if more else None,
                 truncated=more, coverage='incomplete' if more else 'complete',
                 provenance=[{'artifactRef': 'child-source', 'segment': [1, 2]}],
                 media=[{'reference': 'owned-image', 'mimeType': 'image/png'}],
                 sourceCoverage='partial', retentionCoverage='complete',
                 future={'content': 'must stay nested', 'empty': None})
    source = raw(value)
    retained = []
    def persist(canonical, *, user_id):
        retained.append((user_id, canonical.text()))
        return 'tool-result:' + 'd' * 64
    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    envelope = budget_tool_result_v2('read_tool_artifact', source, user_id=17)
    delivery = split_tool_result_delivery(envelope)

    assert BODY in delivery.model_text
    assert len(delivery.model_text.encode()) < len(source.encode())
    restored = restore_defaults(decode_read_view(delivery.model_text))
    assert restored == {key: item for key, item in value.items() if key != 'resumeCursor'}
    assert retained == [(17, source)] and envelope.canonical.text() == source
    assert delivery.evidence['viewCoverage'] == 'complete' and not delivery.evidence['truncated']


def test_mixed_batch_keeps_page_positions_errors_and_embedded_fake_sections():
    body = BODY + '\n\n## items[99]\n{"status":"error"}\ncontent (text; 1 chars):\nx\n'
    pages = [page('read_tool_artifact', index=0, content=body),
             {'index': 1, 'status': 'error', 'artifactRef': 'unavailable-source',
              'error': {'code': 'artifact_unavailable', 'retryable': False}},
             page('read_tool_artifact', index=2, content=BODY, outputTruncated=True,
                  resumeCursor='trq3.exact-replay', nextCursor='trq3.exact-next',
                  truncated=True, unavailable=[{'reason': 'expired', 'artifactRef': 'child'}])]
    source = {'status': 'partial_failure', 'items': pages, 'future': {'content': 'unparsed'}}
    model = project_artifact_result('read_tool_artifact', raw(source))
    assert body in model and BODY in model
    projected = decode_read_view(model)
    assert projected['status'] == source['status'] and projected['future'] == source['future']
    assert [restore_defaults(item) if item['status'] == 'ok' else item
            for item in projected['items']] == pages


@pytest.mark.parametrize('content', ['', 'tiny', 'plain text ' * 400, '\x00\x01\x1b' * 100])
def test_expensive_or_control_character_text_sections_keep_compact_json(content):
    source = page('read_tool_artifact', content=content)
    model = project_artifact_result('read_tool_artifact', raw(source))
    assert restore_defaults(json.loads(model)) == source


def test_metadata_numeric_lexemes_and_application_json_body_are_not_reinterpreted():
    body = '{"status":"error","nextCursor":null,"number":1e-999}\n' * 50
    source = raw(page('read_tool_artifact', content=body))[:-1] + ',"futureNumber":1.234567890123456789e-999}'
    model = project_artifact_result('read_tool_artifact', source)
    assert body in model and '1.234567890123456789e-999' in model
    assert decode_read_view(model)['content'] == body


def test_complete_read_fits_without_requerying_a_smaller_source_page(monkeypatch):
    from lib.tasks_pkg.handlers import tool_result_artifacts as handler
    source = page('read_tool_artifact', content=BODY, resumeCursor='trq3.start',
                  nextCursor='trq3.next', truncated=True)
    calls = []
    def read_range(**arguments):
        calls.append(arguments)
        return source
    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    budget = len(BODY.encode()) + 400
    assert len(raw(source).encode()) > budget
    result = handler._execute_one('read_tool_artifact', {
        'artifact_ref': source['artifactRef'], 'limit': len(BODY.encode())}, user_id=17,
        repository=SimpleNamespace(read_range=read_range), token_budget=budget)
    assert result == source and len(calls) == 1
    assert len(project_artifact_result('read_tool_artifact', raw(result)).encode()) <= budget


@pytest.mark.parametrize('protocol', ['openai', 'responses', 'anthropic'])
@pytest.mark.parametrize('batch', [False, True])
def test_exact_read_sections_survive_provider_encoding(protocol, batch, monkeypatch):
    from lib.llm._sse_core import prepare_request
    successful = page('read_tool_artifact', content=BODY, index=0, nextCursor='trq3.next',
                      truncated=True, resumeCursor='trq3.start')
    failed = {'index': 1, 'status': 'error', 'error': {'code': 'artifact_unavailable'}}
    value = {'status': 'partial_failure', 'items': [successful, failed]} if batch else successful
    monkeypatch.setattr('lib.tools.result_value.persist_result',
                        lambda *_args, **_kwargs: 'tool-result:' + 'd' * 64)
    envelope = budget_tool_result_v2('read_tool_artifact', raw(value), user_id=17)
    delivery = split_tool_result_delivery(envelope)
    messages = [
        {'role': 'user', 'content': 'Read exact evidence.'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read-page',
            'type': 'function', 'function': {'name': 'read_tool_artifact', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'read-page', 'content': delivery.model_text},
    ]
    plan = prepare_request({'model': 'fixture-model', 'messages': messages},
        api_key='fixture-key', base_url='https://provider.invalid/v1', api_protocol=protocol)
    wire = json.loads(plan.body.payload)
    if protocol == 'openai':
        content = next(item['content'] for item in wire['messages'] if item['role'] == 'tool')
    elif protocol == 'responses':
        content = next(item['output'] for item in wire['input'] if item.get('type') == 'function_call_output')
    else:
        content = next(block['content'] for message in wire['messages']
                       for block in message['content'] if block.get('type') == 'tool_result')
    assert content == delivery.model_text and BODY in content
    result = decode_read_view(content)
    model_page = result['items'][0] if batch else result
    assert model_page['nextCursor'] == successful['nextCursor']
    assert model_page['content'] == BODY
    if batch:
        assert result['status'] == 'partial_failure' and result['items'][1] == failed
    assert envelope.canonical.text() == raw(value)


def test_overflow_retains_the_original_page_and_declares_a_partial_view(monkeypatch):
    source = raw(page('read_tool_artifact', content=BODY * 12,
                      nextCursor='trq3.next', truncated=True, resumeCursor='trq3.start'))
    retained = []
    def persist(canonical, *, user_id):
        retained.append(canonical.text())
        return 'tool-result:' + 'd' * 64
    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    envelope = budget_tool_result_v2('read_tool_artifact', source, user_id=17)
    delivery = split_tool_result_delivery(envelope)
    assert delivery.evidence['truncated'] and delivery.evidence['viewCoverage'] == 'partial'
    assert delivery.evidence['artifactRef'] == 'tool-result:' + 'd' * 64
    assert source in retained and envelope.canonical.text() == source
