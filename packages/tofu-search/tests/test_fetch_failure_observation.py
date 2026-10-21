"""Public failure facts exclude internal exception text and response headers."""
import pytest

from tofu_search.fetch.diagnostics import fetch_failure_observation

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('reason', ['timeout', 'spa_shell', 'pdf_extraction_error',
                                  'http_403', 'http_429', 'http_503'])
def test_known_failure_observations_keep_only_supported_public_facts(reason):
    diagnostics = {'reason': reason, 'http_status': 403, 'detail': 'PRIVATE_DETAIL',
                   'retry_after': 'PRIVATE_HEADER', 'caller': 'PRIVATE_CALLER'}
    before = dict(diagnostics)
    result = fetch_failure_observation(diagnostics)
    assert result['reason'] == reason
    assert result.get('http_status') == (int(reason[5:]) if reason.startswith('http_') else None)
    assert isinstance(result['message'], str) and result['message']
    assert 'PRIVATE' not in str(result)
    assert diagnostics == before


@pytest.mark.parametrize('reason', [None, [], {}, True, 'http_99', 'http_600',
                                  'http_403 PRIVATE', 'PRIVATE_UNKNOWN'])
def test_unknown_failure_observations_are_generic(reason):
    result = fetch_failure_observation({'reason': reason, 'detail': 'PRIVATE_DETAIL'})
    assert set(result) == {'message'}
    assert 'PRIVATE' not in result['message']


def test_missing_diagnostics_have_a_generic_observation():
    assert fetch_failure_observation(None) == fetch_failure_observation({})
