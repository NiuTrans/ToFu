"""Executable model-only gateway projection fidelity and byte-saving contract."""
import json

import pytest

from lib.tools.gateway_result_projection import project_gateway_result


pytestmark = pytest.mark.unit

def receipt(**fields):
    return {'name': 'read_files', 'call_id': 'child-1', 'status': 'done',
            'approval': {'required': False, 'status': 'not_required'},
            'duration': 52, 'source': 'execute_calls', 'raw_output_bytes': 500,
            'output_bytes': 500, 'canonical': {'private': True}, 'resultId': 'r1',
            'output_truncated': False, 'output': 'hello', **fields}


def source(children=None, **fields):
    return json.dumps({'status': 'ok', 'results': [receipt()] if children is None else children,
                       'warnings': [], **fields}, ensure_ascii=False)


def project(text):
    result = project_gateway_result('execute_tools', text)
    assert result is not None
    return result


def test_success_strips_only_declared_child_controls_without_mutating_source():
    text = source()
    original = json.loads(text)
    projected = project(text)
    assert json.loads(text) == original
    for key in ('duration', 'source', 'approval', 'raw_output_bytes', 'output_bytes',
                'canonical', 'resultId'):
        assert f'"{key}"' not in projected
    assert '"name":"read_files"' in projected
    assert '"call_id":"child-1"' in projected
    assert '"status":"done"' in projected
    assert '"output_truncated":false' in projected
    assert projected.endswith('output (text; 5 chars):\nhello')
    assert len(projected.encode()) < len(text.encode())


@pytest.mark.parametrize('body', ['hello', 'first\nsecond\n', 'quote " \\ tab\t Unicode 中文',
                                  '{"status":"error","duration":7,"value":9007199254740993}',
                                  '{"results":[],"warnings":[],"status":"ok"}',
                                  '## results[9]\noutput (text; 0 chars):\n'])
def test_text_is_exact_and_never_reinterpreted(body):
    projected = project(source([receipt(output=body)]))
    assert projected.endswith(f'output (text; {len(body)} chars):\n' + body)


def test_structured_output_and_program_keep_json_types_and_numeric_tokens():
    text = ('{"status":"ok","warnings":[],"results":[{"name":"app","call_id":"1",'
            '"status":"done","output":{"duration":1.234567890123456789,'
            '"source":"app","canonical":{"x":true},"resultId":"app",'
            '"large":9007199254740993123456789,"zero":-0,"exp":1e-999,'
            '"values":[null,false,"123"]}}],"program":{"status":"ok",'
            '"result":{"n":9.999999999999999999,"s":"{\\\"x\\\":1}"},'
            '"resultRef":"program_ref"}}')
    projected = project(text)
    for token in ('1.234567890123456789', '9007199254740993123456789', '-0', '1e-999',
                  '9.999999999999999999', '"source":"app"', '"resultId":"app"',
                  '"values":[null,false,"123"]', '"resultRef":"program_ref"'):
        assert token in projected
    assert json.loads(projected.splitlines()[0])['program']['result']['s'] == '{"x":1}'


def test_mixed_status_warnings_errors_authorization_delegation_and_order():
    children = [receipt(call_id=str(i), status=status, error='denied\nreason',
                        approval={'required': True, 'status': 'rejected'},
                        permission={'allowed': False},
                        delegation={'kind': 'same_response_direct_call', 'direct_call_id': 'd1'})
                for i, status in enumerate(('done', 'error', 'rejected', 'aborted', 'delegated'))]
    projected = project(source(children, status='partial_failure',
                               warnings=['repair warning'], errors=[{'code': 'unknown'}]))
    assert '"status":"partial_failure"' in projected
    assert '"warnings":["repair warning"]' in projected
    assert '"errors":[{"code":"unknown"}]' in projected
    for i, status in enumerate(('done', 'error', 'rejected', 'aborted', 'delegated')):
        assert f'## results[{i}]' in projected
        assert f'"call_id":"{i}","status":"{status}"' in projected
    assert '"permission":{"allowed":false}' in projected
    assert '"approval":{"required":true,"status":"rejected"}' in projected
    assert '"direct_call_id":"d1"' in projected
    assert 'error (text; 13 chars):\ndenied\nreason' in projected


def test_truncation_recovery_repairs_and_unknown_metadata_survive():
    projected = project(source([receipt(output_truncated=True, canonicalRef='tool-result:r1',
        artifactRef='artifact:1', continuation={'cursor': 'next'}, retentionError='quota',
        normalization={'repairs': ['unwrapped arguments']}, future={'duration': 7},
        approval={'required': False, 'status': 'not_required', 'future': True})],
        future={'source': 'application'}, duration=99, programRef='program:1'))
    for token in ('"output_truncated":true', 'tool-result:r1', 'artifact:1', '"cursor":"next"',
                  '"retentionError":"quota"', 'unwrapped arguments', '"future":{"duration":7}',
                  '"future":{"source":"application"}', '"programRef":"program:1"',
                  '"duration":99', '"approval":'):
        assert token in projected


def test_incomplete_output_keeps_capture_identity_without_a_ready_reference():
    projected = project(source([receipt(output_truncated=True)]))
    assert '"resultId":"r1"' in projected


def test_empty_children_does_not_imply_success():
    projected = project(source([], status='error', errors=[{'code': 'invalid_call'}]))
    assert '"status":"error"' in projected
    assert 'results: []' in projected
    assert 'invalid_call' in projected
    assert '"status":"ok"' not in projected


@pytest.mark.parametrize('media', [
    [{'type': 'image', 'data': 'EXACT', 'mimeType': 'image/png'}],
    {'contentBlocks': [{'type': 'text', 'text': 'caption'}, {'type': 'audio', 'data': 'EXACT'}]},
    {'__screenshot__': True, 'data': 'EXACT'},
    {'image_url': {'url': 'data:image/png;base64,EXACT'}},
])
def test_media_passes_through(media):
    text = source([receipt(output=media)])
    assert project_gateway_result('execute_tools', text) is None


@pytest.mark.parametrize('text', [
    'not json', '[]', '{"status":"ok","results":[]}',
    '{"status":"ok","results":[],"warnings":[],"status":"error"}',
    '{"status":"ok","results":[],"warnings":[],"value":NaN}',
    '{"status":"ok","results":[],"warnings":[],"value":Infinity}',
    '{"contractVersion":"tofu.tool-result/v2","status":"error","results":[],"warnings":[]}',
    '{"status":"ok","results":[{"output":"unidentified"}],"warnings":[]}',
])
def test_uncertain_or_typed_failure_is_not_projected(text):
    assert project_gateway_result('execute_tools', text) is None


@pytest.mark.parametrize('name', ['execute_program', 'app', 'search_tools', ''])
def test_arbitrary_application_envelopes_and_program_values_are_not_admitted(name):
    assert project_gateway_result(name, source()) is None


def test_multiline_byte_savings_are_bounded_and_measurable():
    text = source([receipt(output='line "quoted" \\ path\n' * 200)])
    projected = project(text)
    before, after = len(text.encode()), len(projected.encode())
    assert 0 < after < before
    assert after / before < 0.85
