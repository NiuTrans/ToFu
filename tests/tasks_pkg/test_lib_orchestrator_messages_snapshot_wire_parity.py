"""Canonical preflight evidence preserves the request and diagnostic fallback."""
from copy import deepcopy
import pytest

pytestmark = pytest.mark.unit


def emit_snapshot(monkeypatch, *, messages=None, prepared=None, tools=None):
    from lib.tasks_pkg.orchestrator import _messages_snapshot as owner
    events = []
    monkeypatch.setattr(owner, 'append_event', lambda task, event: events.append(event))
    task = {'id': 'snapshot-task', '_userId': 71, 'config': {}}
    owner.emit_messages_snapshot_event(task, messages if messages is not None else [
        {'role': 'user', 'content': 'inspect evidence'}], tid='snapshot', round_num=0,
        model='fixture', thinking_enabled=None, thinking_depth='max', preset='',
        temperature=.3, max_tokens=1024, response_format=None,
        tools=tools, prepared_messages=prepared)
    return events


def test_request_uses_canonical_evidence_and_full_parameter_contract(monkeypatch):
    event, = emit_snapshot(monkeypatch)
    assert event['type'] == 'messages_snapshot' and event['kind'] == 'request'
    assert event['model'] == 'fixture'
    assert event['params'] == {'maxTokens': 1024, 'temperature': .3,
        'thinkingEnabled': None, 'thinkingDepth': 'max', 'preset': '',
        'responseFormat': None, 'stream': True}
    assert event['inspectorRef'] == {'taskId': 'snapshot-task', 'roundNum': 1, 'turn': event['turn']}


def test_failed_build_is_labeled_and_preserves_original_messages(monkeypatch):
    messages = [{'role': 'user', 'content': 'verbatim evidence', '_temporary': 'private'}]
    before = deepcopy(messages)
    event, = emit_snapshot(monkeypatch, messages=messages)
    assert messages == before
    assert event['degraded'] and event['degradedReason'] == 'request-build-fallback'
    assert event['messages'][0]['content'] == 'verbatim evidence'
    assert '_temporary' not in event['messages'][0]


def test_prepared_body_skips_second_sanitize_but_strips_snapshot_sidecars(monkeypatch):
    import lib.tasks_pkg.wire_messages as wire
    import lib.tasks_pkg.manager._events as events
    monkeypatch.setattr(wire, 'apply_wire_sanitize', lambda *a, **kw: pytest.fail('double sanitize'))
    prepared = [{'role': 'user', 'content': 'canonical wire'}]
    stripped = []
    original = events._strip_base64_for_snapshot
    def strip(messages):
        stripped.append(messages)
        return original(messages)
    monkeypatch.setattr(events, '_strip_base64_for_snapshot', strip)
    event, = emit_snapshot(monkeypatch, prepared=prepared)
    assert stripped == [prepared]
    assert event['messages'] == prepared
    assert 'degraded' not in event


@pytest.mark.parametrize('tools', [None, [], [{'type': 'function', 'function': {'name': 'probe'}}]])
def test_tools_are_attached_only_when_present(monkeypatch, tools):
    event, = emit_snapshot(monkeypatch, tools=tools)
    assert ('tools' in event) == bool(tools)
    if tools:
        assert event['tools'] == tools


def test_diagnostic_failure_is_observable_without_replacing_preparation_error(monkeypatch, caplog):
    import lib.tasks_pkg.wire_messages as wire
    def fail(*a, **kw):
        raise RuntimeError('synthetic snapshot fault')
    monkeypatch.setattr(wire, 'apply_wire_sanitize', fail)
    assert emit_snapshot(monkeypatch) == []
    assert 'messages_snapshot failed' in caplog.text
