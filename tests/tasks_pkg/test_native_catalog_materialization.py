"""Native catalog isolation and no-call settlement without execution preparation."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.agent_core.native_process import RoundRequest, RoundSettlement, ToolRequest
from lib.tasks_pkg.native_gateway import NativeGateway, compile_native_tool_catalog
from tests.tasks_pkg.test_native_gateway import catalog, principal, task

pytestmark = pytest.mark.unit


def gateway(owner, executor, definitions=None, permissions=None):
    return NativeGateway(principal=principal(), task=owner, executor=executor,
        configuration={}, tool_list=catalog() if definitions is None else definitions,
        permissions=permissions, project_path=None, project_enabled=False,
        model='fixture', round_source=lambda: 0, decision_policy=None,
        pending_calls=8, pending_bytes=16384)


def settlement(owner, calls, number=0):
    request = RoundRequest(principal(), owner['id'], 'round', number, 0)
    return RoundSettlement(request, {'kind': 'returned', 'model_calls': calls})


def test_text_round_retires_previous_declarations_without_execution_contract_work(monkeypatch):
    owner = task()
    with ThreadPoolExecutor(max_workers=1) as executor:
        port = gateway(owner, executor, permissions={'write_file': 'write'})
        calls = [{'id': 'write', 'type': 'function', 'function': {'name': 'write_file',
            'arguments': json.dumps({'path': 'file.txt', 'content': 'data'})}}]
        port.prepare_response(settlement(owner, calls))
        assert port.response_retained_bytes > 0
        def unavailable(*args, **kwargs):
            raise AssertionError('text-only response should not prepare tool execution')
        monkeypatch.setattr('lib.tools.contracts.compile_execution_contract_documents', unavailable)
        port.prepare_response(settlement(owner, [], number=1))
        assert port.response_retained_bytes == 0 and not owner['toolRounds']
        occurrence = ToolRequest(principal(), owner['id'], 'callback', 'write_file',
            {'path': 'file.txt', 'content': 'data'}, model_round=0)
        with pytest.raises(ValueError):
            port._parse_occurrence(occurrence, 0)
        port.close()


def test_empty_round_still_refuses_pending_approval(monkeypatch):
    owner = task()
    with ThreadPoolExecutor(max_workers=1) as executor:
        port = gateway(owner, executor, permissions={'write_file': 'write'})
        # An already-owned approval cannot disappear when the model sends text.
        from lib.tasks_pkg.native_gateway import _PendingApproval
        port._pending['reserved'] = _PendingApproval(b'proof')
        with pytest.raises(ValueError, match='overlaps pending approval'):
            port.prepare_response(settlement(owner, [], number=1))
        assert 'reserved' in port._pending
        port.close()


def test_catalog_input_and_each_export_are_independent():
    owner = task()
    definitions = catalog()
    expected = compile_native_tool_catalog(owner, definitions)
    with ThreadPoolExecutor(max_workers=1) as executor:
        port = gateway(owner, executor, definitions)
        definitions[0]['function']['parameters']['properties']['outside'] = {'type': 'boolean'}
        first = port.native_catalog()
        first['write_file']['arguments_schema']['required'].append('changed')
        assert port.native_catalog() == expected
        assert port.permissions == {name: row['permission'] for name, row in expected.items()}
        port.close()


@pytest.mark.parametrize('corruption', ['nan', 'infinity', 'cycle', 'duplicate', 'strict', 'decoded'])
def test_default_permission_admission_still_validates_full_catalog(corruption):
    definitions = catalog()
    if corruption in ('nan', 'infinity'):
        definitions[0]['function']['parameters']['maximum'] = float('nan' if corruption == 'nan' else 'inf')
    elif corruption == 'cycle':
        parameters = definitions[0]['function']['parameters']
        parameters['cycle'] = parameters
    elif corruption == 'duplicate':
        definitions += copy.deepcopy(definitions)
    elif corruption == 'strict':
        definitions[0]['function']['strict'] = True
    else:
        definitions[0]['decoded_result_schema'] = []
    with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises((ValueError, TypeError)):
        gateway(task(), executor, definitions)


def test_catalog_export_normalizes_nested_json_and_observes_current_permissions():
    definitions = catalog()
    parameters = definitions[0]['function']['parameters']
    parameters['examples'] = ({12: ('nested', {'flag': True})},)
    owner = task()
    with ThreadPoolExecutor(max_workers=1) as executor:
        port = gateway(owner, executor, definitions)
        expected = compile_native_tool_catalog(owner, definitions)
        assert port.native_catalog() == expected
        first = port.native_catalog()
        first['write_file']['arguments_schema']['examples'][0]['12'][1]['flag'] = False
        assert port.native_catalog() == expected
        port.permissions['write_file'] = 'approval'
        expected['write_file']['permission'] = 'approval'
        assert port.native_catalog() == expected
        port.close()


def test_catalog_export_rejects_invalid_updated_permissions_and_encoded_values():
    with ThreadPoolExecutor(max_workers=1) as executor:
        port = gateway(task(), executor)
        port.permissions['foreign'] = 'read'
        with pytest.raises(ValueError, match='outside the prepared catalog'):
            port.native_catalog()
        port.permissions.pop('foreign')
        definitions = json.loads(port.tool_list)
        definitions[0]['function']['parameters']['maximum'] = float('inf')
        port.tool_list = json.dumps(definitions)
        with pytest.raises(ValueError):
            port.native_catalog()
        port.close()
