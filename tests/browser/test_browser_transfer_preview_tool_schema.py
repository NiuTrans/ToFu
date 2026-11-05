"""Browser server-file and page-preview model contracts."""

from __future__ import annotations

from jsonschema import Draft7Validator
import pytest

from lib.tools.browser import BROWSER_TOOL_PREVIEW_PAGE
from lib.tools.contracts import (
    ToolContractError,
    compile_execution_contract_documents,
    validate_tool_arguments_from_documents,
)
from lib.tools.gateway import sanitize_wire_tools
from lib.tools.search import build_browser_download_url_to_server_tool


pytestmark = pytest.mark.unit


def _download_tool():
    return build_browser_download_url_to_server_tool()


def _admission_error(tool, arguments):
    documents = compile_execution_contract_documents([tool])
    with pytest.raises(ToolContractError) as rejected:
        validate_tool_arguments_from_documents(
            documents, tool['function']['name'], arguments)
    return rejected.value


def test_download_schema_keeps_location_authority_and_transport_honesty():
    function = _download_tool()['function']
    desc = function['description']

    for phrase in (
            'download/save/install/unzip/export/copy', 'TOFU SERVER',
            'full signed link', 'bounded server HTTP first',
            'selected logged-in browser', 'cookies stay in Chrome',
            'browser_get_cookies', 'curl/wget', 'browser Downloads',
            'device-local file', 'location=server_staging', 'absolute path',
            'byte size', 'SHA-256', 'transport', 'authorized copy/move',
            'verified destination'):
        assert phrase in desc, phrase


def test_download_schema_requires_a_bounded_target_and_passes_preflight():
    tool = _download_tool()
    params = tool['function']['parameters']
    props = params['properties']
    assert params['type'] == 'object'
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)

    assert props['url']['maxLength'] == 8192
    assert props['text']['maxLength'] == 500
    assert props['selector']['maxLength'] == 2048
    documents = compile_execution_contract_documents([tool])
    for args in (
            {'url': 'https://files.test/a.zip?sig=full'},
            {'text': 'Download', 'tab_id': 7},
            {'selector': 'a.release'},
            {'text': 'Download', 'selector': 'a.release'}):
        assert validator.is_valid(args), args
        validate_tool_arguments_from_documents(
            documents, 'browser_download_url_to_server', args)
    # The target choice is an admission rule, not a provider-schema root.
    for args in ({}, {'tab_id': 7}):
        error = _admission_error(tool, args)
        assert error.code == 'missing_required_arguments', args
        assert 'url, text, selector' in str(error), args
        assert 'text/selector' in error.next_action, args
    wire = [tool]
    for model in ('', 'gpt-5.5', 'claude-opus-5.5'):
        assert sanitize_wire_tools(wire, model=model) is wire, model
    kimi_parameters = sanitize_wire_tools(
        wire, model='kimi-k3')[0]['function']['parameters']
    assert kimi_parameters['type'] == 'object'
    assert 'anyOf' not in kimi_parameters


def test_preview_schema_matches_exact_source_and_runtime_bounds():
    params = BROWSER_TOOL_PREVIEW_PAGE['function']['parameters']
    props = params['properties']
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)

    assert (props['width']['minimum'], props['width']['maximum']) == (320, 3840)
    assert (props['height']['minimum'], props['height']['maximum']) == (240, 2160)
    assert (props['wait_ms']['minimum'], props['wait_ms']['maximum']) == (0, 15000)
    assert _admission_error(BROWSER_TOOL_PREVIEW_PAGE, {}).code == (
        'missing_required_arguments')
    both = _admission_error(BROWSER_TOOL_PREVIEW_PAGE, {
        'path': 'index.html', 'url': 'http://localhost/'})
    assert both.code == 'invalid_argument_value'
    assert 'got path, url' in str(both)
    assert 'url for an HTTP(S) page' in both.next_action
    assert validator.is_valid({'path': 'dist/index.html'})
    assert validator.is_valid({
        'url': 'http://127.0.0.1:8080/', 'width': 1280, 'height': 800,
        'full_page': True, 'wait_ms': 1500,
    })
    assert sanitize_wire_tools([BROWSER_TOOL_PREVIEW_PAGE]) == [
        BROWSER_TOOL_PREVIEW_PAGE]


def test_preview_schema_keeps_server_side_diagnostic_boundary():
    desc = BROWSER_TOOL_PREVIEW_PAGE['function']['description']
    for phrase in (
            'headless browser ON THE SERVER', 'screenshot', 'console',
            'uncaught JS errors', 'failed requests', 'after frontend edits',
            'exactly one source', 'project-relative .html path',
            'relative assets/ES modules work', 'external requests are blocked',
            'HTTP(S) url', 'fetch_url', 'browser_read_page',
            "user's browser extension"):
        assert phrase in desc, phrase
