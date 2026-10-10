"""Read fallback formatting never becomes a second content acceptance path."""

import pytest

from lib.browser.handlers._tabs import _render_read_result

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('budget', [0, 1, 16, 100, 50_000])
def test_error_fallback_obeys_total_budget(budget):
    result = _render_read_result(
        {'error': 'Read failed ' * 10_000, 'html': 'RAW_BODY'}, 1,
        max_chars=budget, network_text='NETWORK_BODY')
    assert len(result) <= budget
    assert result.startswith('Error: Read failed'[:budget])
    assert 'RAW_BODY' not in result
    assert 'NETWORK_BODY' not in result


@pytest.mark.parametrize('budget', [0, 1, 32, 128, 4096])
def test_elements_fallback_obeys_total_budget(budget):
    payload = {
        'title': 'Page', 'url': 'https://example.com/',
        'elements': [{'tag': 'button', 'text': 'Visible label ' * 500}] * 200,
        'html': 'RAW_BODY', 'text': 'RAW_TEXT',
    }
    result = _render_read_result(payload, 1, max_chars=budget,
                                 network_text='NETWORK_BODY')
    assert len(result) <= budget
    if budget >= 128:
        assert 'Found 200 element(s)' in result
        assert '[1] <button> Visible label' in result
    assert 'RAW_BODY' not in result
    assert 'RAW_TEXT' not in result
    assert 'NETWORK_BODY' not in result


def test_element_metadata_and_malformed_entries_are_bounded():
    result = _render_read_result({
        'title': 'T' * 100_000, 'url': 'U' * 100_000,
        'elements': [None, {'text': {'body': 'RAW_BODY'}}],
    }, 1, max_chars=120)
    assert len(result) == 120
    assert 'RAW_BODY' not in result


@pytest.mark.parametrize('payload', [
    {'html': '<article>RAW_BODY</article>', 'text': 'RAW_BODY'},
    {'body': {'text': 'RAW_BODY'}, 'url': 'https://example.com/'},
    {'elements': 'RAW_BODY', 'html': 'RAW_BODY'},
    '<html>RAW_BODY</html>',
    [{'body': 'RAW_BODY'}],
    None,
])
def test_body_payload_has_no_raw_fallback(payload):
    result = _render_read_result(payload, 1, network_text='NETWORK_BODY')
    assert result.startswith('Error:')
    assert 'unified content collection' in result
    assert 'RAW_BODY' not in result
    assert 'NETWORK_BODY' not in result


def test_empty_elements_remain_structural_not_body_content():
    result = _render_read_result({
        'elements': [], 'html': 'RAW_BODY', 'text': 'RAW_BODY',
    }, 1)
    assert 'Found 0 element(s)' in result
    assert 'RAW_BODY' not in result


def test_structured_error_is_not_dumped_as_page_content():
    result = _render_read_result({'error': {'body': 'RAW_BODY'}}, 1)
    assert result == 'Error: Browser read failed'
