"""tests/test_mcp_readonly_hint_spelling.py — the readOnlyHint extractor survives
the SDK's field rename.

WHY THIS GUARD EXISTS (measured, 2026-07-29)
--------------------------------------------
``_extract_read_only_hint`` used to read ONE attribute name::

    hint = getattr(annotations, 'readOnlyHint', None)

The WIRE name is always camelCase, so that looks safe. The PYTHON ATTRIBUTE
name is not: MCP SDK v1 exposes the field as ``readOnlyHint``, while v2 moved
every model field to snake_case (``read_only_hint``), keeping camelCase only
as a serialization alias. Measured against both installed SDKs::

    v1: getattr(ann, 'readOnlyHint') -> True    'read_only_hint' -> ABSENT
    v2: getattr(ann, 'readOnlyHint') -> ABSENT  'read_only_hint' -> True

WHY THE FAILURE IS EXPENSIVE AND INVISIBLE
-------------------------------------------
The single-spelling lookup does not raise on the other SDK — it returns False
for EVERY tool. And False is also the honest answer for a server that declares
no hints, so the broken state is indistinguishable from "nobody annotated
anything". What it silently changes: ``lib/tasks_pkg/tool_dispatch/_flags.py``
puts every non-read-only MCP tool into the WRITE partition, i.e. serial
dispatch plus an approval prompt in manual mode. So on a v2 SDK every
correctly-annotated read-only tool in the fleet would quietly leave the
parallel pool and start asking the user for permission, with nothing in any
log to say why.

This is a defect INDEPENDENT of when we upgrade: it is the same
"judgement anchored to a name someone else owns" shape as the ``McpError`` →
``MCPError`` rename that silently disabled the degraded-health gate.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.mcp.client._coerce import _extract_read_only_hint  # noqa: E402

pytestmark = pytest.mark.unit


class _Ann:
    """A ToolAnnotations stand-in exposing exactly ONE spelling."""

    def __init__(self, attr, value):
        setattr(self, attr, value)


class _Tool:
    def __init__(self, annotations, name='t'):
        self.annotations = annotations
        self.name = name


# ── The rename axis ──────────────────────────────────────────────────

@pytest.mark.parametrize('attr', ['readOnlyHint', 'read_only_hint'])
def test_true_is_read_under_either_spelling(attr):
    """v1 (camelCase) and v2 (snake_case) must both resolve to True.

    Before the fix only the v1 spelling matched, so a v2 SDK silently
    reclassified every read-only tool as a write.
    """
    assert _extract_read_only_hint(_Tool(_Ann(attr, True))) is True


@pytest.mark.parametrize('attr', ['readOnlyHint', 'read_only_hint'])
def test_false_stays_false_under_either_spelling(attr):
    assert _extract_read_only_hint(_Tool(_Ann(attr, False))) is False


def test_the_installed_sdk_is_actually_exercised():
    """Drive the REAL ``ToolAnnotations`` of whichever SDK is installed.

    Without this the suite could pass entirely on hand-built stand-ins while
    the shipped model uses a third spelling.
    """
    from mcp.types import Tool, ToolAnnotations
    tool = Tool(name='x', description='d', inputSchema={'type': 'object'},
                annotations=ToolAnnotations(readOnlyHint=True))
    assert _extract_read_only_hint(tool) is True

    tool_w = Tool(name='x', description='d', inputSchema={'type': 'object'},
                  annotations=ToolAnnotations(readOnlyHint=False))
    assert _extract_read_only_hint(tool_w) is False


def test_the_stand_ins_model_a_real_sdk_shape():
    """Guard-of-the-guard: at least one spelling must match the live SDK.

    If the SDK ever renames the field a third time, the parametrized cases
    above would keep passing on stale stand-ins while production broke.
    """
    from mcp.types import ToolAnnotations
    fields = set(ToolAnnotations.model_fields)
    assert fields & {'readOnlyHint', 'read_only_hint'}, (
        f'installed SDK exposes neither known spelling: {sorted(fields)} — '
        f'update _extract_read_only_hint AND this guard')


# ── Conservative defaults must survive ───────────────────────────────

def test_absent_annotations_are_not_read_only():
    assert _extract_read_only_hint(_Tool(None)) is False


def test_unrelated_attribute_is_not_mistaken_for_the_hint():
    """Only the two known spellings count — no fuzzy matching."""
    assert _extract_read_only_hint(_Tool(_Ann('readonly', True))) is False
    assert _extract_read_only_hint(_Tool(_Ann('isReadOnly', True))) is False


def test_non_true_values_are_not_read_only():
    """Only an explicit True qualifies; truthy junk must not."""
    for junk in ('true', 1, [1], object()):
        assert _extract_read_only_hint(_Tool(_Ann('readOnlyHint', junk))) is False


def test_raw_dict_wire_form_is_understood():
    """Some transports hand us the parsed JSON rather than a model."""
    assert _extract_read_only_hint(_Tool({'readOnlyHint': True})) is True
    assert _extract_read_only_hint(_Tool({'readOnlyHint': False})) is False
    assert _extract_read_only_hint(_Tool({})) is False


def test_tool_without_annotations_attribute_at_all():
    assert _extract_read_only_hint(object()) is False
