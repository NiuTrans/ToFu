"""CallToolResult structuredContent is preserved across SDK spellings.

The v2 SDK renamed ``structuredContent`` → ``structured_content``; a tool that
returns only a structured payload (no text content block) must not come back
as an empty string. Merge with text is single-rule and dedupes the common
"text block == serialized structuredContent" echo, and the merged result still
respects ``MCP_MAX_RESULT_CHARS``.
"""

import asyncio
from types import SimpleNamespace

import pytest

from lib.mcp.client._bridge import MCPBridge
from lib.mcp.types import MCP_MAX_RESULT_CHARS

pytestmark = pytest.mark.unit


class _Text:
    def __init__(self, text):
        self.text = text


def _result(content=None, structured_content=None, structuredContent=None):
    return SimpleNamespace(
        content=content or [],
        structured_content=structured_content,
        structuredContent=structuredContent,
    )


# ── structuredContent extraction: dual spelling ─────────────────────────

@pytest.mark.parametrize('attr', ['structured_content', 'structuredContent'])
def test_structured_content_serializes_either_spelling(attr):
    payload = {'ok': True, 'rows': [{'id': 1}]}
    text = MCPBridge._extract_structured_content(_result(**{attr: payload}))
    assert text is not None
    assert 'rows' in text
    assert 'ok' in text


def test_snake_case_wins_when_both_spellings_are_exposed():
    result = _result(structured_content={'v': 'snake'},
                     structuredContent={'v': 'camel'})
    assert '"snake"' in MCPBridge._extract_structured_content(result)
    assert 'camel' not in MCPBridge._extract_structured_content(result)


def test_absent_and_empty_structured_content_is_none():
    assert MCPBridge._extract_structured_content(_result()) is None
    assert MCPBridge._extract_structured_content(_result(structured_content={})) is None
    assert MCPBridge._extract_structured_content(_result(structuredContent=[])) is None


# ── Single-rule merge with dedupe ────────────────────────────────────────

def test_merge_keeps_text_only():
    assert MCPBridge._merge_result_parts('hello', None) == 'hello'
    assert MCPBridge._merge_result_parts('hello', '') == 'hello'


def test_merge_returns_structured_when_text_is_blank():
    assert MCPBridge._merge_result_parts('', '{"rows": [1]}') == '{"rows": [1]}'
    assert MCPBridge._merge_result_parts('   ', '{"rows": [1]}') == '{"rows": [1]}'


def test_merge_appends_structured_when_it_adds_information():
    out = MCPBridge._merge_result_parts('done', '{"rows": [1, 2, 3]}')
    assert out == 'done\n\n[Structured result]\n{"rows": [1, 2, 3]}'


def test_merge_dedupes_text_echo_of_structured_content():
    payload = '{"rows": [1, 2, 3]}'
    assert MCPBridge._merge_result_parts(payload, payload) == payload


# ── async_call_tool integration + budget ─────────────────────────────────

class _Session:
    def __init__(self, result):
        self._result = result

    async def call_tool(self, tool_name, arguments=None,
                        read_timeout_seconds=None):
        return self._result


def _call(result):
    bridge = MCPBridge()
    handle = SimpleNamespace(name='s', sdk_generation=0,
                             session=_Session(result))
    return asyncio.run(bridge._async_call_tool(handle, 't', {}, None))


def test_async_call_surfaces_structured_only_result():
    out = _call(_result(structured_content={'rows': [1, 2, 3]}))
    assert '[1, 2, 3]' in out


def test_async_call_merges_text_and_structured_without_duplication():
    payload = {'rows': [1, 2, 3]}
    serialized = MCPBridge._extract_structured_content(
        _result(structured_content=payload))
    out = _call(_result(content=[_Text(serialized)],
                        structured_content=payload))
    assert out == serialized


def test_async_call_enforces_max_result_chars_budget():
    huge = {'blob': 'x' * (MCP_MAX_RESULT_CHARS + 1000)}
    out = _call(_result(structured_content=huge))
    assert len(out) <= MCP_MAX_RESULT_CHARS + 200
    assert '[Truncated:' in out
