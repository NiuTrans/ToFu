"""Direct contracts for the shared orchestration event registry."""

import pytest

from lib.orchestration.events import (
    EVENT_PREVIEW_CHARS,
    EVENT_SCHEMA,
    EVENT_TIMELINE_PREVIEW_CHARS,
    event_preview,
    event_gate_effect,
    runtime_event_contract,
    runtime_event_contract_schema,
)


pytestmark = pytest.mark.unit


def test_event_contract_owns_wire_and_timeline_preview_limits():
    contract = runtime_event_contract()

    assert contract['schema'] == EVENT_SCHEMA
    assert contract['previewLimits'] == {
        'wire': EVENT_PREVIEW_CHARS,
        'timeline': EVENT_TIMELINE_PREVIEW_CHARS,
    }
    assert event_preview('x' * (EVENT_PREVIEW_CHARS + 5)) == (
        'x' * EVENT_PREVIEW_CHARS)


def test_event_contract_snapshots_are_detached():
    first = runtime_event_contract()
    first['previewLimits']['wire'] = 1
    first['types']['step_complete']['timeline'] = False

    fresh = runtime_event_contract()
    assert fresh['previewLimits']['wire'] == EVENT_PREVIEW_CHARS
    assert fresh['types']['step_complete']['timeline'] is True


def test_event_contract_openapi_schema_is_derived_and_future_tolerant():
    contract = runtime_event_contract()
    schema = runtime_event_contract_schema()
    properties = schema['properties']
    types = properties['types']

    assert schema['required'] == ['schema', 'previewLimits', 'types']
    assert properties['schema']['enum'] == [contract['schema']]
    assert properties['previewLimits']['required'] == ['wire', 'timeline']
    assert properties['previewLimits']['properties']['wire']['const'] == \
        EVENT_PREVIEW_CHARS
    assert types['required'] == list(contract['types'])
    assert types['additionalProperties'] is True
    assert types['properties']['step_phase']['properties']['durable'] == {
        'type': 'boolean', 'const': False,
    }
    assert types['properties']['human_request']['properties'][
        'runStatus']['enum'] == ['paused']
    assert contract['types']['human_request']['gateEffect'] == 'open'
    assert contract['types']['human_resolved']['gateEffect'] == 'close'
    assert event_gate_effect('human_request') == 'open'
    assert event_gate_effect('human_resolved') == 'close'
    assert event_gate_effect('step_start') == ''

    types['required'].append('client-event')
    assert 'client-event' not in runtime_event_contract_schema()[
        'properties']['types']['required']
