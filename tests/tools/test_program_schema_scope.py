"""Both program entry points describe the same existing execution authority."""
import pytest

from lib.tools.gateway import execute_program_schema, execute_tools_schema

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("schema", [
    pytest.param(execute_program_schema, id="code-action"),
    pytest.param(execute_tools_schema, id="legacy-program"),
])
def test_program_description_explains_inherited_authority_and_effect_route(schema):
    description = schema()["function"]["description"].lower()
    assert "permissions" in description
    assert "approvals" in description
    assert "no imports/timers/files/processes/network" in description
    assert "effects use tools.*" in description


def test_json_batch_description_keeps_its_own_execution_surface():
    schema = execute_tools_schema(include_program=False)["function"]
    assert set(schema["parameters"]["properties"]) == {"calls", "execution"}
    assert "permissions/approvals apply" in schema["description"].lower()
    assert "imports" not in schema["description"].lower()
