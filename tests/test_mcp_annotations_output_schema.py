"""MCP tool annotations and outputSchema are exposed across SDK spellings.

The v2 SDK renamed ``annotations.readOnlyHint`` → ``read_only_hint`` (and every
other ToolAnnotations field to snake_case) and ``Tool.outputSchema`` →
``output_schema``. The catalog snapshot must carry the full four-hint
annotation set plus the output schema without depending on one SDK spelling,
while the execution partition keeps keying off ``read_only_hint`` alone.
"""

from types import SimpleNamespace

import pytest

from lib.mcp.client._bridge import MCPBridge
from lib.mcp.client._coerce import (
    _extract_annotations,
    _extract_output_schema,
    _extract_read_only_hint,
)

pytestmark = pytest.mark.unit

_ALL_FALSE = {
    'readOnlyHint': False,
    'destructiveHint': False,
    'idempotentHint': False,
    'openWorldHint': False,
}


class _Ann:
    """A ToolAnnotations stand-in exposing arbitrary spellings."""

    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _Tool:
    def __init__(self, annotations=None, output_schema=None,
                 outputSchema=None, name='t', description='d'):
        self.annotations = annotations
        self.output_schema = output_schema
        self.outputSchema = outputSchema
        self.name = name
        self.description = description
        self.input_schema = {'type': 'object', 'properties': {}}
        self.inputSchema = self.input_schema


# ── annotations: four hints, dual spelling, conservative default ────────

@pytest.mark.parametrize('spelling', ['camel', 'snake'])
def test_annotations_expose_all_four_hints_under_either_spelling(spelling):
    if spelling == 'camel':
        ann = _Ann(readOnlyHint=True, destructiveHint=False,
                   idempotentHint=True, openWorldHint=False)
    else:
        ann = _Ann(read_only_hint=True, destructive_hint=False,
                   idempotent_hint=True, open_world_hint=False)
    assert _extract_annotations(_Tool(annotations=ann)) == {
        'readOnlyHint': True,
        'destructiveHint': False,
        'idempotentHint': True,
        'openWorldHint': False,
    }


def test_annotations_default_all_false_when_absent():
    assert _extract_annotations(_Tool(annotations=None)) == _ALL_FALSE


def test_annotations_accept_raw_dict_wire_form():
    out = _extract_annotations(
        _Tool(annotations={'readOnlyHint': True, 'destructiveHint': True}))
    assert out['readOnlyHint'] is True
    assert out['destructiveHint'] is True
    assert out['idempotentHint'] is False
    assert out['openWorldHint'] is False


def test_read_only_hint_matches_annotations_read_only_hint():
    tool = _Tool(annotations=_Ann(readOnlyHint=True, destructiveHint=True))
    assert _extract_read_only_hint(tool) is True
    assert _extract_annotations(tool)['readOnlyHint'] is True


# ── outputSchema: dual spelling ──────────────────────────────────────────

@pytest.mark.parametrize('attr', ['output_schema', 'outputSchema'])
def test_output_schema_reads_either_spelling(attr):
    schema = {'type': 'object', 'properties': {'a': {'type': 'string'}}}
    assert _extract_output_schema(_Tool(**{attr: schema})) == schema


def test_output_schema_defaults_empty_dict_when_absent_or_non_dict():
    assert _extract_output_schema(_Tool()) == {}
    assert _extract_output_schema(_Tool(output_schema='not a dict')) == {}


# ── catalog snapshot + bridge integration ───────────────────────────────

def test_replace_server_catalog_populates_annotations_and_output_schema():
    bridge = MCPBridge()
    tool = _Tool(
        annotations=_Ann(readOnlyHint=True, destructiveHint=True,
                         idempotentHint=True, openWorldHint=False),
        output_schema={'type': 'object', 'properties': {}},
    )
    handle = SimpleNamespace(catalog_fingerprint='', tools=None,
                             catalog_version='')
    changed = bridge._replace_server_catalog('s', handle, [tool])
    assert changed is True

    info = bridge._tool_index['mcp__s__t']
    assert info['read_only_hint'] is True
    assert info['annotations']['readOnlyHint'] is True
    assert info['annotations']['destructiveHint'] is True
    assert info['annotations']['idempotentHint'] is True
    assert info['annotations']['openWorldHint'] is False
    assert info['output_schema'] == {'type': 'object', 'properties': {}}


def test_catalog_snapshot_surfaces_annotations_and_output_schema():
    bridge = MCPBridge()
    bridge._tool_index['mcp__s__t'] = {
        'server_name': 's', 'tool_name': 't',
        'namespaced_name': 'mcp__s__t',
        'description': 'd', 'input_schema': {},
        'openai_def': {'type': 'function',
                       'function': {'name': 'mcp__s__t'}},
        'read_only_hint': True,
        'annotations': {
            'readOnlyHint': True, 'destructiveHint': True,
            'idempotentHint': False, 'openWorldHint': False,
        },
        'output_schema': {'type': 'object', 'properties': {}},
        'meta': {}, 'schema_hash': 'h', 'catalog_version': 'v',
    }
    rows = bridge.get_tool_catalog_snapshot()
    assert len(rows) == 1
    row = rows[0]
    assert row['read_only_hint'] is True
    assert row['annotations']['readOnlyHint'] is True
    assert row['annotations']['destructiveHint'] is True
    assert row['output_schema'] == {'type': 'object', 'properties': {}}
