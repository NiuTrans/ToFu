"""JS title metadata is observed on the executed route, never model output."""
from __future__ import annotations

from unittest.mock import patch
import pytest

from lib.browser.handlers._capture import _handle_execute_js
from lib.browser.tab_titles import _reset_for_tests, ingest_tab_list
from lib.browser.tool_runtime import BrowserToolRuntime

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_title_cache():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_cached_title_is_pre_execution_and_owner_device_scoped():
    ingest_tab_list('42', 'chosen', [{'id': 71, 'title': 'Before title'}])
    ingest_tab_list('42', 'other', [{'id': 71, 'title': 'Wrong device'}])
    calls = []
    metadata = {}

    def send(command, params, **authority):
        calls.append((command, params, authority))
        if command == 'list_tabs':
            return [{'id': 71, 'title': 'Before title'}], None
        ingest_tab_list('42', 'chosen', [{'id': 71, 'title': 'After title'}])
        return {'value': 2}, None

    runtime = BrowserToolRuntime('42', 'chosen', sender=send, display_metadata=metadata)
    assert _handle_execute_js({'tabId': 71, 'code': 'document.title="After"; 2'}, runtime) == '{\n  "value": 2\n}'
    assert metadata == {'tabTitle': 'Before title'}
    assert [c[0] for c in calls] == ['list_tabs', 'execute_js']
    assert calls[0][2]['client_id'] == 'chosen'
    assert calls[0][2]['owner_user_id'] == '42'


def test_cold_cache_uses_one_existing_list_tabs_then_executes_once():
    calls = []
    metadata = {}

    def send(command, params, **authority):
        calls.append(command)
        if command == 'list_tabs':
            return [{'id': 71, 'title': 'T' * 300}, {'id': 99, 'title': 'Other tab'}], None
        return 2, None

    runtime = BrowserToolRuntime('42', 'chosen', sender=send, display_metadata=metadata)
    args = {'tabId': 71, 'code': '1 + 1'}
    assert _handle_execute_js(args, runtime) == '2'
    assert calls == ['list_tabs', 'execute_js']
    assert metadata == {'tabTitle': 'T' * 240}
    assert args == {'tabId': 71, 'code': '1 + 1'}


def test_missing_title_is_advisory_and_does_not_block_or_retry_js():
    calls = []
    metadata = {}

    def send(command, params, **authority):
        calls.append(command)
        return (None, 'unavailable') if command == 'list_tabs' else ('ok', None)

    runtime = BrowserToolRuntime('42', 'chosen', sender=send, display_metadata=metadata)
    assert _handle_execute_js({'tabId': 71, 'code': '1'}, runtime) == 'ok'
    assert calls == ['list_tabs', 'execute_js']
    assert metadata == {}


def test_calls_without_metadata_do_not_add_display_only_bridge_requests():
    calls = []

    def send(command, params, **authority):
        calls.append(command)
        return None, None

    runtime = BrowserToolRuntime('42', 'chosen', sender=send)
    assert _handle_execute_js({'tabId': 71, 'code': '1'}, runtime) == 'Executed successfully (no return value)'
    assert calls == ['execute_js']


def test_title_is_persisted_in_result_metadata_not_arguments_or_content():
    from lib.tasks_pkg.handlers.browser import _handle_browser_tool
    captured = {}
    args = {'tab_id': 71, 'code': '1 + 1'}

    def execute(name, fn_args, **kwargs):
        assert name == 'browser_execute_js'
        assert fn_args is args
        kwargs['display_metadata']['tabTitle'] = 'Human page title'
        return '2'

    def settle(task, rn, row, results, **kwargs):
        captured['results'] = results

    with patch('lib.browser.dispatch.execute_browser_tool', side_effect=execute), \
         patch('lib.tasks_pkg.handlers.browser._finalize_tool_round', side_effect=settle):
        result = _handle_browser_tool({'_userId': 42}, None, 'browser_execute_js', 'call', args,
                                     1, {}, {'browserClientId': 'chosen'}, '', False)
    assert result == ('call', '2', False)
    assert captured['results'][0]['tabTitle'] == 'Human page title'
    assert 'tabTitle' not in args


def test_display_title_honors_pinned_device_and_working_scope():
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    ingest_tab_list('42', 'chosen', [{'id': 71, 'title': 'Correct page'}])
    ingest_tab_list('42', 'other', [{'id': 71, 'title': 'Wrong device'}])
    task = {'_userId': 42, '_cfg': {'browserClientId': 'chosen', 'browserWorkKey': 'workspace'}}
    clients = [{'client_id': 'chosen'}, {'client_id': 'other'}]
    with patch('lib.browser.queue.get_connected_clients', return_value=clients), \
         patch('lib.browser._resolve.current_work_tab', return_value=71) as current:
        _, row, _ = _build_tool_round_entry('browser_execute_js', {'code': '1'}, 'call', '{}',
                                          0, False, task=task)
    assert row['query'] == 'Execute JS in "Correct page"'
    current.assert_called_once_with(('42', 'chosen', 'workspace'))
    assert row['toolArgs'] == '{}'


def test_display_never_uses_disconnected_pinned_device_or_other_owner():
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    ingest_tab_list('42', 'other', [{'id': 71, 'title': 'Wrong device'}])
    task = {'_userId': 42, '_cfg': {'browserClientId': 'disconnected'}}
    with patch('lib.browser.queue.get_connected_clients', return_value=[{'client_id': 'other'}]):
        _, row, _ = _build_tool_round_entry('browser_execute_js', {'tab_id': 71}, 'call', '{}',
                                          0, False, task=task)
    assert row['query'] == 'Execute JS in tab'
    assert 'Wrong device' not in row['query'] and '71' not in row['query']


def test_working_tab_title_uses_the_executed_work_scope():
    from lib.browser._resolve import forget_work_tab, remember_work_tab
    route = ('42', 'chosen', 'own-workspace')
    remember_work_tab(route, 72)
    metadata = {}
    calls = []

    def send(command, params, **authority):
        calls.append((command, params))
        if command == 'list_tabs':
            return [{'id': 71, 'title': 'Active but wrong', 'active': True},
                    {'id': 72, 'title': 'Working page'}], None
        return 'ok', None

    try:
        runtime = BrowserToolRuntime('42', 'chosen', sender=send, work_key='own-workspace',
                                     display_metadata=metadata)
        assert _handle_execute_js({'code': '1'}, runtime) == 'ok'
        assert metadata == {'tabTitle': 'Working page'}
        assert calls[-1] == ('execute_js', {'tabId': 72, 'code': '1'})
    finally:
        forget_work_tab(route, 72)


def test_owner_miss_and_failed_title_lookup_never_skip_or_duplicate_js():
    ingest_tab_list('99', 'chosen', [{'id': 71, 'title': 'Other owner'}])
    calls = []
    metadata = {}

    def send(command, params, **authority):
        calls.append(command)
        if command == 'list_tabs':
            raise RuntimeError('optional title lookup failed')
        return 'JS Error: actual execution error', None

    runtime = BrowserToolRuntime('42', 'chosen', sender=send, display_metadata=metadata)
    assert _handle_execute_js({'tabId': 71, 'code': 'throw Error()'}, runtime) == 'JS Error: actual execution error'
    assert calls == ['list_tabs', 'execute_js']
    assert metadata == {}


def test_astral_title_respects_browser_units_without_splitting_character():
    ingest_tab_list('42', 'chosen', [{'id': 71, 'title': 'x' + '😀' * 200}])
    metadata = {}
    runtime = BrowserToolRuntime('42', 'chosen', sender=lambda *a, **k: (1, None),
                                 display_metadata=metadata)
    assert _handle_execute_js({'tabId': 71, 'code': '1'}, runtime) == '1'
    assert metadata['tabTitle'] == 'x' + '😀' * 119
    assert len(metadata['tabTitle'].encode('utf-16-le')) <= 480


def test_error_settlement_and_lazy_snapshot_preserve_title_metadata():
    from lib.tasks_pkg.handlers.browser import _handle_browser_tool
    from lib.turn_projection_segments import snapshot_with_reference_tool_segments

    from lib.tools.result_envelope import typed_tool_error
    task = {'_userId': 42}
    row = {'toolCallId': 'js-call', 'toolName': 'browser_execute_js', 'roundNum': 1,
           'query': 'Execute JS in tab', 'status': 'searching',
           'toolArgs': {'tab_id': 71, 'code': 'throw Error()'}}
    events = []

    def execute(*args, **kwargs):
        kwargs['display_metadata']['tabTitle'] = 'Observed error page'
        return typed_tool_error('browser_execution_failed', retryable=False,
                                next_action='Inspect the JavaScript error.',
                                message='JS Error: actual execution error')

    with patch('lib.browser.dispatch.execute_browser_tool', side_effect=execute), \
         patch('lib.tasks_pkg.executor._finalize.append_event', side_effect=lambda task, event: events.append(event)):
        result = _handle_browser_tool(task, None, 'browser_execute_js', 'js-call',
                                     row['toolArgs'], 1, row, {'browserClientId': 'chosen'}, '', False)
    assert result[0] == 'js-call' and result[2] is False
    assert 'JS Error: actual execution error' in result[1].to_model_text()
    assert row['status'] == 'error'
    assert row['results'][0]['tabTitle'] == 'Observed error page'
    assert events[0]['results'][0]['tabTitle'] == 'Observed error page'
    snapshot = {'conversationId': 'fixture', 'turns': [
        {'turnId': 'turn', 'status': 'completed', 'projectionRevision': 3,
         'projection': {'toolRounds': [row], 'segments': []}}
    ]}
    # The JS renderer needs its specialized output, even in lazy history.
    row['toolContent'] = 'JS Error: full diagnostic\n' * 300
    projected = snapshot_with_reference_tool_segments(snapshot, owner_cache_scope='a' * 24,
                                                     lazy_tool_results=True)
    saved = projected['turns'][0]['projection']['toolRounds'][0]
    assert saved['results'][0]['tabTitle'] == 'Observed error page'
    assert saved['toolContent'] == row['toolContent']
    assert '_snapshotToolResult' not in saved


def test_current_page_title_replaces_stale_cached_title_before_js():
    ingest_tab_list('42', 'chosen', [{'id': 71, 'title': 'Old navigation page'}])
    calls = []
    metadata = {}

    def send(command, params, **authority):
        calls.append(command)
        if command == 'list_tabs':
            return [{'id': 71, 'title': 'Current page'}], None
        return {'__error': True, 'message': 'failure on current page'}, None

    runtime = BrowserToolRuntime('42', 'chosen', sender=send, display_metadata=metadata)
    assert _handle_execute_js({'tabId': 71, 'code': 'throw Error()'}, runtime) == 'JS Error: failure on current page'
    assert metadata == {'tabTitle': 'Current page'}
    assert calls == ['list_tabs', 'execute_js']


def test_tab_resolution_list_is_reused_without_another_display_request():
    from lib.browser._resolve import forget_work_tab
    calls = []
    metadata = {}

    def send(command, params, **authority):
        calls.append(command)
        if command == 'list_tabs':
            return [{'id': 73, 'title': 'Resolved automation page', 'active': True,
                     'automation': True, 'isClient': False}], None
        return 2, None

    runtime = BrowserToolRuntime('42', 'chosen', sender=send, work_key='unique-active-test',
                                 display_metadata=metadata)
    try:
        assert _handle_execute_js({'code': '1+1'}, runtime) == '2'
        assert metadata == {'tabTitle': 'Resolved automation page'}
        assert calls == ['list_tabs', 'execute_js']
    finally:
        forget_work_tab(runtime.route_key, 73)


def test_result_display_metadata_contract_keeps_legacy_results_compatible():
    from lib.conversation_sync.validation import ContractViolation, decode
    legacy = {'results': ['text', 2, False, None, ['nested'], {'arbitrary': 1}, {'tabTitle': 'Current page'}]}
    assert decode('TurnToolRound', legacy) is legacy
    with pytest.raises(ContractViolation):
        decode('TurnToolRound', {'results': [{'tabTitle': 'T' * 241}]})
    with pytest.raises(ContractViolation):
        decode('TurnToolRound', {'results': [{'tabTitle': 71}]})
