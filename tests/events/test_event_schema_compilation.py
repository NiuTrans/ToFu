"""Shared stream/push schemas parse once without caching validation results."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from lib.agent_core import events


pytestmark = pytest.mark.unit


@pytest.fixture(params=['stream', 'push'])
def schema_boundary(request, monkeypatch):
    """Use fresh immutable contracts, including registry replacement fixtures."""
    event_type = 'schema_compilation_probe'
    if request.param == 'stream':
        registry = events._BY_TYPE
        base = events.EventSpec(event_type, 'test', 'schema probe')
        validate = events.validate_event
    else:
        registry = events._PUSH_BY_TYPE
        base = events.PushFrameSpec(event_type, 'test', 'test', 'schema probe')
        validate = events.validate_push_frame

    def install(schema):
        contract = replace(base, schema=schema)
        monkeypatch.setitem(registry, event_type, contract)
        return contract

    return event_type, install, validate


@pytest.mark.parametrize('kind,accepted', [
    ('str', {6}),
    ('bool', {0, 1}),
    ('int', {2, 3}),
    ('number', {2, 3, 4}),
    ('dict', {7}),
    ('list', {8}),
    ('None', {5}),
    ('int | None', {2, 3, 5}),
    ('bool | int', {0, 1, 2, 3}),
    ('number | bool', {0, 1, 2, 3, 4}),
    ('list | str', {6, 8}),
    ('unknown', set()),
    ('unknown | int', {2, 3}),
    ('unknown | None', {5}),
    (' int | int ', {2, 3}),
])
def test_kind_alternatives_keep_json_types(schema_boundary, kind, accepted):
    event_type, install, validate = schema_boundary
    install((events.FieldSpec('value', kind, required=True),))
    values = [False, True, 0, 1, 1.5, None, 'text', {}, [], (), b'bytes']
    for index, value in enumerate(values):
        violations = validate({'type': event_type, 'value': value})
        assert bool(violations) == (index not in accepted), (kind, value)
        if violations:
            assert violations == (
                f"field 'value' expects {kind}, got {type(value).__name__}",)


def test_violation_order_and_final_mutations_remain_visible(schema_boundary):
    event_type, install, validate = schema_boundary
    install((
        events.FieldSpec('count', 'int', required=True),
        events.FieldSpec('name', 'str', required=True),
        events.FieldSpec('final', 'bool', required=True),
    ))
    frame = {'type': event_type, 'count': 3, 'name': 'valid', 'final': False}
    assert validate(frame) == ()
    frame['count'] = True
    frame['surprise'] = 1
    del frame['name']
    del frame['final']
    violations = validate(frame)
    assert violations[0] == "field 'count' expects int, got bool"
    assert violations[1].startswith("undeclared field 'surprise' (")
    assert violations[2:] == (
        "missing required field 'name'", "missing required field 'final'")


def test_replacing_a_contract_uses_its_new_schema(schema_boundary):
    event_type, install, validate = schema_boundary
    install((events.FieldSpec('value', 'int'),))
    frame = {'type': event_type, 'value': 1}
    assert validate(frame) == ()
    install((events.FieldSpec('value', 'str'),))
    assert validate(frame) == ("field 'value' expects str, got int",)
    frame['value'] = 'new contract'
    assert validate(frame) == ()
    install(None)
    assert validate({'type': event_type, 'extra': object()}) == ()


def test_each_contract_parses_kinds_once_not_once_per_frame(schema_boundary):
    event_type, install, validate = schema_boundary
    calls = []

    class CountedKind(str):
        def split(self, *args, **kwargs):
            calls.append(self)
            return super().split(*args, **kwargs)

    install((events.FieldSpec('value', CountedKind('int | None')),))
    for value in (1, None, False, 2, 'invalid'):
        validate({'type': event_type, 'value': value})
    assert calls == ['int | None']


def test_concurrent_first_reads_do_not_share_frame_results(schema_boundary):
    event_type, install, validate = schema_boundary
    install((events.FieldSpec('value', 'int', required=True),))
    ready = Barrier(2)

    def read(value):
        ready.wait(timeout=5)
        return validate({'type': event_type, 'value': value})

    with ThreadPoolExecutor(max_workers=2) as executor:
        valid = executor.submit(read, 3)
        invalid = executor.submit(read, False)
        assert valid.result(timeout=5) == ()
        assert invalid.result(timeout=5) == ("field 'value' expects int, got bool",)
