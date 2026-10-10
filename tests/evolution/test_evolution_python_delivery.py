"""Code delivery is separate from runtime correctness and scientific validity."""
import json
import pytest
from evaluations.evolution.python_delivery import check_python_delivery,MAX_CODE_BYTES
pytestmark = pytest.mark.unit


def response(source,**fields):
    return {'status':'done','content':json.dumps({'python':source}),**fields}


def test_original_invalid_cache_program_refuses_before_execution():
    result=check_python_delivery(response('import collections, random, time, \nprint(.dumps({}))'))
    assert result['reason']=='syntax_error' and result['line']==1
    assert result['executed'] is False


def test_compile_never_executes_or_proves_behavior(tmp_path):
    marker=tmp_path/'unexpected-write'
    result=check_python_delivery(response(f'open({str(marker)!r}, "w").write("bad")'))
    assert result['outcome']=='pass' and result['reason']=='compilation_only'
    assert result['executed'] is False and not marker.exists()


@pytest.mark.parametrize('status',['incomplete','error'])
def test_incomplete_code_cannot_pass_even_when_syntax_valid(status):
    assert check_python_delivery(response('pass',status=status))['reason']=='response_not_completed'


def test_duplicate_keys_and_surrounding_prose_refuse():
    for answer in ['{"python":"pass","python":"pass"}', '{"python":"pass"} prose']:
        assert check_python_delivery({'status':'done','content':answer})['reason']=='invalid_json'


def test_oversized_multibyte_code_refuses():
    assert check_python_delivery(response('#'+ '🧪'*(MAX_CODE_BYTES//4+1)))['reason']=='code_empty_or_exceeds_bound'


def test_non_utf8_source_is_a_typed_refusal():
    assert check_python_delivery(response('\ud800'))['reason']=='source_not_utf8'
