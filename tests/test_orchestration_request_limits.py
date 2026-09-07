"""Cross-layer guards for backend-owned orchestration input limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.orchestration.authoring_contract import authoring_contract
from lib.orchestration._definition_contract import MAX_NAME_LEN, MAX_NODES
from lib.orchestration._subflow_contract import MAX_SUBFLOW_DEPTH
from lib.orchestration.request_limit_contract import (
    MAX_COMPOSE_HISTORY_ITEMS,
    MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
    MAX_COMPOSE_REQUIREMENT_LENGTH,
    MAX_HUMAN_INPUT_LENGTH,
    MAX_RUN_INPUT_LENGTH,
    request_limits_contract,
)
from routes.api_v1.orchestration_authoring_http import compose_request_schema
from routes.api_v1.orchestration_definition_request_http import (
    definition_selection_request_schema,
)
from routes.api_v1.orchestration_mutation_http import human_input_request_schema


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_request_limits_drive_authoring_contract_and_openapi_schemas():
    limits = request_limits_contract()
    assert limits == {
        'definitionName': {'maxLength': MAX_NAME_LEN},
        'definitionNodes': {'maxItems': MAX_NODES},
        'subflowDepth': {'maxDepth': MAX_SUBFLOW_DEPTH},
        'composeRequirement': {
            'maxLength': MAX_COMPOSE_REQUIREMENT_LENGTH,
        },
        'composeHistory': {
            'retainedItems': MAX_COMPOSE_HISTORY_ITEMS,
            'messageMaxLength': MAX_COMPOSE_HISTORY_CONTENT_LENGTH,
        },
        'runInput': {'maxLength': MAX_RUN_INPUT_LENGTH},
        'humanInput': {'maxLength': MAX_HUMAN_INPUT_LENGTH},
    }
    assert authoring_contract()['requestLimits'] == limits
    assert definition_selection_request_schema()['properties'][
        'definition']['properties']['name']['maxLength'] == limits[
            'definitionName']['maxLength']
    assert definition_selection_request_schema()['properties'][
        'definition']['properties']['nodes']['maxItems'] == limits[
            'definitionNodes']['maxItems']
    assert compose_request_schema()['properties']['requirement'][
        'maxLength'] == limits['composeRequirement']['maxLength']
    assert compose_request_schema()['properties']['history'][
        'x-retainedItems'] == limits['composeHistory']['retainedItems']
    assert compose_request_schema()['properties']['history']['items'][
        'properties']['content']['maxLength'] == limits[
            'composeHistory']['messageMaxLength']
    assert definition_selection_request_schema(include_input=True)[
        'properties']['input']['maxLength'] == limits['runInput']['maxLength']
    assert human_input_request_schema()['properties']['response'][
        'maxLength'] == limits['humanInput']['maxLength']
    assert human_input_request_schema()['properties']['response'][
        'minLength'] == 1


def test_request_limit_documents_are_detached_and_routes_do_not_redeclare_them():
    first = request_limits_contract()
    first['definitionName']['maxLength'] = 1
    first['definitionNodes']['maxItems'] = 1
    first['subflowDepth']['maxDepth'] = 1
    first['runInput']['maxLength'] = 1
    assert request_limits_contract()['definitionName'][
        'maxLength'] == MAX_NAME_LEN
    assert request_limits_contract()['definitionNodes'][
        'maxItems'] == MAX_NODES
    assert request_limits_contract()['subflowDepth'][
        'maxDepth'] == MAX_SUBFLOW_DEPTH
    assert request_limits_contract()['runInput']['maxLength'] == 8000
    assert authoring_contract()['requestLimits']['runInput']['maxLength'] == 8000

    for relative in (
        'routes/api_v1/orchestration_authoring_http.py',
        'routes/api_v1/orchestration_definition_request_http.py',
        'routes/api_v1/orchestration_mutation_http.py',
    ):
        source = (ROOT / relative).read_text(encoding='utf-8')
        assert 'MAX_COMPOSE_REQUIREMENT_LENGTH = ' not in source
        assert 'MAX_RUN_INPUT_LENGTH = ' not in source
        assert 'MAX_HUMAN_INPUT_LENGTH = ' not in source


def test_definition_contract_has_one_physical_backend_owner():
    from lib.orchestration._definition_contract import SCHEMA_ID
    from lib.orchestration.wire_formats import DEFINITION_FORMAT

    contract = (ROOT / 'lib/orchestration/_definition_contract.py').read_text()
    wire_formats = (ROOT / 'lib/orchestration/wire_formats.py').read_text()
    validator = (ROOT / 'lib/orchestration/_validate.py').read_text()
    consumers = {
        relative: (ROOT / relative).read_text()
        for relative in (
            'lib/orchestration/_builtin_definitions.py',
            'lib/orchestration/_subflow_expansion.py',
            'lib/orchestration/authoring_contract.py',
            'lib/orchestration/definition_inspection.py',
            'lib/orchestration/request_limit_contract.py',
            'lib/orchestration_composer.py',
        )
    }

    assert "DEFINITION_FORMAT = 'tofu.orchestration/v1'" in wire_formats
    assert SCHEMA_ID == DEFINITION_FORMAT
    assert "SCHEMA_ID = 'tofu.orchestration/v1'" not in contract
    assert 'NODE_TYPE_ORDER =' in contract
    assert 'MAX_NAME_LEN = 120' in contract
    assert 'MAX_NODES = 200' in contract
    assert "SCHEMA_ID = 'tofu.orchestration/v1'" not in validator
    assert 'MAX_NAME_LEN =' not in validator
    assert 'MAX_NODES =' not in validator
    assert 'from lib.orchestration._definition_contract import (' in validator
    for source in consumers.values():
        assert 'lib.orchestration._definition_contract import' in source
    assert contract.count('\n') < 30
    assert validator.count('\n') < 250
