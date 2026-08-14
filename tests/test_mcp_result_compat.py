"""MCP result parsing works before and after the SDK v2 field rename."""

from types import SimpleNamespace

import pytest

from lib.mcp.client._bridge import _tool_result_is_error

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('result', 'expected'), [
    (SimpleNamespace(is_error=True), True),
    (SimpleNamespace(is_error=False), False),
    (SimpleNamespace(isError=True), True),
    (SimpleNamespace(isError=False), False),
    (SimpleNamespace(), False),
])
def test_tool_result_error_flag_accepts_both_sdk_spellings(result, expected):
    assert _tool_result_is_error(result) is expected


def test_snake_case_field_wins_when_both_are_exposed():
    # Some compatibility models expose an alias as well as the canonical
    # attribute.  The active SDK generation's canonical field must win.
    result = SimpleNamespace(is_error=False, isError=True)
    assert _tool_result_is_error(result) is False
