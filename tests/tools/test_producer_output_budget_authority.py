"""Producer output sizing belongs to shared L0, not model-selected budgets."""


import pytest

pytestmark = pytest.mark.unit
from lib.browser.workflow_contract import WORKFLOW_SCHEMA, WORKFLOW_TOOL_SCHEMA
from lib.skills.tools import READ_SKILL_RESOURCE_TOOL
from lib.tools.browser import BROWSER_TOOL_READ_PAGE, BROWSER_TOOL_RESEARCH_PAGE
from lib.tools.project import PROJECT_TOOL_REPO_MAP
from lib.tools.search import build_update_search_settings_tool


def _property_names(schema):
    if isinstance(schema, dict):
        yield from schema.get('properties', {})
        for value in schema.values():
            yield from _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _property_names(value)


def test_producers_have_no_model_output_budget_port():
    forbidden = {'max_tokens', 'max_chars', 'max_chars_search',
                 'max_chars_direct', 'max_chars_pdf', 'output_limit',
                 'token_budget', 'char_limit'}
    tools = [PROJECT_TOOL_REPO_MAP, BROWSER_TOOL_READ_PAGE,
             BROWSER_TOOL_RESEARCH_PAGE, READ_SKILL_RESOURCE_TOOL,
             build_update_search_settings_tool()]
    for tool in tools:
        function = tool['function']
        assert not forbidden.intersection(_property_names(function['parameters'])), function['name']
    for schema in (WORKFLOW_SCHEMA, WORKFLOW_TOOL_SCHEMA):
        assert not forbidden.intersection(_property_names(schema))


def test_scope_and_collection_bounds_are_not_output_budgets():
    assert {'path', 'focus'} <= PROJECT_TOOL_REPO_MAP['function']['parameters']['properties'].keys()
    assert {'max_pages', 'max_scrolls', 'pagination'} <= BROWSER_TOOL_RESEARCH_PAGE['function']['parameters']['properties'].keys()
    assert 'cursor' in READ_SKILL_RESOURCE_TOOL['function']['parameters']['properties']
    assert 'timeout_s' in WORKFLOW_SCHEMA['properties']
