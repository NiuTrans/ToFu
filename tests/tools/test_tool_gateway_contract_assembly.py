"""Gateway and MCP names retain executable ToolContractV2 documents."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_explicit_benchmark_tools_have_exact_executable_contracts():
    from evaluations.swebench.tofu_kimi_runtime import tofu_kimi_clean_tool_schemas
    from lib.tasks_pkg.model_config import _assemble_tool_list
    from lib.tools.contracts import ToolContractError, validate_tool_arguments_from_documents

    cfg = {'_explicitToolSchemas': tofu_kimi_clean_tool_schemas(),
           '_toolContractDocumentsByName': {'stale_foreign_tool': {}}}
    tools, enabled = _assemble_tool_list(cfg, None, False, 'explicit-fixture',
                                        'off', False, False, False, False, False)
    assert enabled
    documents = cfg['_toolContractDocumentsByName']
    assert set(documents) == {t['function']['name'] for t in tools}
    assert validate_tool_arguments_from_documents(documents, 'custom__run_command',
        {'command': 'printf ready', 'timeout_sec': 1800})['timeout_sec'] == 1800
    assert validate_tool_arguments_from_documents(documents, 'custom__submit_result',
        {'summary': 'done', 'validation_command': 'test -f output'})['summary'] == 'done'
    with pytest.raises(ToolContractError):
        validate_tool_arguments_from_documents(documents, 'custom__run_command',
                                               {'command': 'printf ready', 'timeout_sec': 1801})
    with pytest.raises(ToolContractError):
        validate_tool_arguments_from_documents(documents, 'stale_foreign_tool', {})


def test_provider_safe_mcp_names_allow_hyphens():
    from lib.tools.contracts import ToolContractV2

    contract = ToolContractV2(
        name='mcp__12306-train__get-current-date',
        parameters={'type': 'object', 'properties': {}},
        model_description='Get the current railway date.',
    )
    assert contract.provider_schema()['function']['name'] == contract.name


@pytest.mark.parametrize('name', [
    'mcp server tool',
    'mcp/server/tool',
    'mcp.server.tool',
    '工具',
    'mcp\nserver',
])
def test_provider_unsafe_tool_names_still_fail_closed(name):
    from lib.tools.contracts import ToolContractV2

    with pytest.raises(ValueError, match='ASCII letters'):
        ToolContractV2(
            name=name,
            parameters={'type': 'object', 'properties': {}},
            model_description='unsafe',
        )


def test_gateway_contracts_are_attached_when_schemas_become_executable():
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        _attach_gateway_contract_documents,
    )
    from lib.tools.contracts import validate_tool_arguments_from_documents

    direct_document = {'contractVersion': 'tofu.tool-contract/v2'}
    task = {
        '_toolContractDocumentsByName': {'direct_tool': direct_document},
    }
    _attach_gateway_contract_documents(task, 'local')

    assert set(task['_tool_gateway_names']) == {'execute_tools', 'execute_program', 'search_tools'}
    assert task['_toolContractDocumentsByName']['direct_tool'] is direct_document
    assert validate_tool_arguments_from_documents(
        task['_toolContractDocumentsByName'],
        'search_tools',
        {'query': 'current date'},
    ) == {'query': 'current date', 'limit': 8}
    assert validate_tool_arguments_from_documents(
        task['_toolContractDocumentsByName'],
        'execute_tools',
        {'calls': []},
    ) == {'calls': [], 'execution': 'auto'}
    assert validate_tool_arguments_from_documents(
        task['_toolContractDocumentsByName'], 'execute_program',
        {'code': 'return 1;'},
    )['code'] == 'return 1;'


def test_execute_contract_remains_available_when_search_is_off():
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        _attach_gateway_contract_documents,
    )

    task = {'_toolContractDocumentsByName': {}}
    _attach_gateway_contract_documents(task, 'off')
    assert set(task['_tool_gateway_names']) == {'execute_tools', 'execute_program'}
    assert set(task['_toolContractDocumentsByName']) == {'execute_tools', 'execute_program'}


def test_gateway_contract_compile_failure_disables_gateway_without_raising(
        monkeypatch):
    import lib.tools.contracts as contracts
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        _attach_gateway_contract_documents,
    )

    direct_document = {'contractVersion': 'tofu.tool-contract/v2'}
    task = {
        '_toolContractDocumentsByName': {'direct_tool': direct_document},
    }

    def _fail_compile(*_args, **_kwargs):
        raise ValueError('derived gateway contract is invalid')

    monkeypatch.setattr(
        contracts, 'compile_execution_contract_documents', _fail_compile)

    attached = _attach_gateway_contract_documents(task, 'local')

    assert attached is False
    assert task['_tool_gateway_names'] == []
    assert task['_toolContractDocumentsByName'] == {
        'direct_tool': direct_document}


def test_round_assembly_degrades_contract_defect_to_text_only(monkeypatch):
    import lib.tasks_pkg.orchestrator._tool_assembly_prep as prep

    direct_tool = {
        'type': 'function',
        'function': {
            'name': 'read_doc',
            'description': 'Read a document.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }
    monkeypatch.setattr(
        prep, '_assemble_tool_list',
        lambda *_args, **_kwargs: ([direct_tool], True))
    monkeypatch.setattr(
        prep, '_attach_gateway_contract_documents',
        lambda *_args, **_kwargs: False)
    task = {'id': 'task-text-only', 'convId': 'conv-text-only', 'messages': []}
    mcfg = {
        'project_path': '', 'project_enabled': False,
        'search_mode': 'multi', 'search_enabled': True,
        'fetch_enabled': True, 'code_exec_enabled': False,
        'browser_enabled': False, 'desktop_enabled': False,
        'image_gen_enabled': False, 'human_guidance_enabled': False,
        'scheduler_enabled': False,
    }

    tools, has_real_tools = prep.assemble_round_tools({}, task, mcfg)

    assert tools == []
    assert has_real_tools is False
    assert task['_tool_gateway_names'] == []
    assert task['_tool_schema'] == []
    assert task['_executable_tool_catalog'] == []
    assert task['_toolSearchMode'] == 'off'
    assert task['_toolSearchCatalogSize'] == 0
    assert task['_toolSearchableCount'] == 0
    assert task['_toolContractDocumentsByName'] == {}
    assert task['_frontendSelectedToolNames'] == []


def test_round_assembly_degrades_registry_defect_to_text_only(monkeypatch):
    import lib.tasks_pkg.orchestrator._tool_assembly_prep as prep

    def _fail_assembly(*_args, **_kwargs):
        raise RuntimeError('plugin schema builder failed')

    monkeypatch.setattr(prep, '_assemble_tool_list', _fail_assembly)
    task = {
        'id': 'task-registry-defect',
        'convId': 'conv-registry-defect',
        'messages': [],
        '_tool_schema': [{'stale': True}],
        '_toolContractDocumentsByName': {'stale': {'authority': True}},
    }
    mcfg = {
        'project_path': '', 'project_enabled': False,
        'search_mode': 'multi', 'search_enabled': True,
        'fetch_enabled': True, 'code_exec_enabled': False,
        'browser_enabled': False, 'desktop_enabled': False,
        'image_gen_enabled': False, 'human_guidance_enabled': False,
        'scheduler_enabled': False,
    }

    tools, has_real_tools = prep.assemble_round_tools({}, task, mcfg)

    assert tools == []
    assert has_real_tools is False
    assert task['_tool_schema'] == []
    assert task['_executable_tool_catalog'] == []
    assert task['_toolContractDocumentsByName'] == {}
    assert task['_tool_gateway_names'] == []
    assert task['_toolSearchMode'] == 'off'
