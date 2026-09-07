"""tests/test_event_schema.py — Field-level wire-contract conformance.

``lib/agent_core/events.py`` historically documented payload fields as PROSE
(``EventSpec.fields``). Prose cannot fail CI, so a field could ride the wire
undeclared — the ``rawToolTokens`` incident: emitted by the pipeline,
consumed by the frontend badge, absent from the contract, discovered by a
user. ``EventSpec.schema`` (``FieldSpec`` tuples) makes the field contract
machine-readable, and this suite is its enforcement half:

  A. **Registry hygiene** — every schema'd spec keeps schema ↔ prose in
     EXACT key sync, uses only the closed kind vocabulary, and never
     duplicates a field name. A schema that drifts from its own
     documentation fails here, not in a frontend.
  B. **Construction gate** — ``build_event`` raises
     :class:`EventContractError` on an undeclared field, a missing required
     field, or a type mismatch (strict is the default under pytest);
     ``TOFU_EVENT_SCHEMA=warn/off`` degrade gracefully.
  C. **REAL pipeline conformance** — a scripted ``execute_tool_pipeline``
     round (success AND failure lanes) must emit only conforming frames,
     validated on the FINAL post-mutation shape the recorder sees.
  D. **Delivery seam** — ``manager.append_event`` re-checks the final frame
     and reports violations to listeners.

If this fails
-------------
You changed a schema'd event's shape. Update the ``FieldSpec`` tuple AND the
prose ``fields`` map in ``lib/agent_core/events.py`` together (the sync test
demands both) — and on a *breaking* change bump ``EVENT_CONTRACT_VERSION``.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from lib.agent_core.events import (
    FIELD_KINDS,
    EventContractError,
    EventType,
    FieldSpec,
    add_event_violation_listener,
    all_event_specs,
    build_event,
    check_event,
    get_event_spec,
    remove_event_violation_listener,
    validate_event,
)

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════
#  A. Registry hygiene — schema ↔ prose can never drift apart
# ═══════════════════════════════════════════════════════════════════

def _schema_specs():
    return [spec for spec in all_event_specs() if spec.schema is not None]


def test_pilot_event_is_schema_driven():
    """tool_complete is the pilot: the incident event is the first gated one."""
    spec = get_event_spec(EventType.TOOL_COMPLETE)
    assert spec is not None and spec.schema is not None
    required = {f.name for f in spec.schema if f.required}
    assert required == {'roundNum', 'toolCallId', 'toolName', 'toolContent'}


def test_schema_field_names_match_prose_fields_exactly():
    """The rawToolTokens class, killed structurally: a field declared in the
    machine schema but undocumented in prose (or vice versa) fails CI."""
    for spec in _schema_specs():
        schema_names = {f.name for f in spec.schema}
        prose_names = set(spec.fields)
        assert schema_names == prose_names, (
            f'{spec.type}: schema/prose drift — '
            f'schema-only: {sorted(schema_names - prose_names)}, '
            f'prose-only: {sorted(prose_names - schema_names)}')


def test_schema_uses_only_the_closed_kind_vocabulary():
    for spec in _schema_specs():
        for field_spec in spec.schema:
            alternatives = {a.strip() for a in field_spec.kind.split('|')}
            assert alternatives and alternatives <= FIELD_KINDS, (
                f'{spec.type}.{field_spec.name}: unknown kind(s) '
                f'{sorted(alternatives - FIELD_KINDS)} — a typo here would '
                'fail closed at emission time; extend FIELD_KINDS '
                'deliberately instead')


def test_schema_has_no_duplicate_field_names():
    for spec in _schema_specs():
        names = [f.name for f in spec.schema]
        assert len(names) == len(set(names)), f'{spec.type}: duplicate names'


def test_clock_stamped_schema_events_declare_the_stamp():
    """build_event auto-stamps ``emittedAt`` on clocked types; a schema that
    forgot it would raise on every conforming emission."""
    from lib.agent_core.events import _CLOCK_STAMPED_TYPES
    for spec in _schema_specs():
        if spec.type in _CLOCK_STAMPED_TYPES:
            assert 'emittedAt' in {f.name for f in spec.schema}, spec.type


# ═══════════════════════════════════════════════════════════════════
#  B. Construction gate — build_event enforces the schema
# ═══════════════════════════════════════════════════════════════════

def _valid_minimal_kwargs() -> dict:
    return {'roundNum': 1, 'toolCallId': 'tc-1', 'toolName': 'read_files',
            'toolContent': 'ok'}


def test_valid_minimal_construction_passes():
    event = build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs())
    assert validate_event(event) == ()
    # Auto-stamped clock fields conform without caller effort.
    assert isinstance(event['emittedAt'], (int, float))


def test_valid_full_construction_passes():
    event = build_event(
        EventType.TOOL_COMPLETE,
        ** _valid_minimal_kwargs(),
        toolTokens=120, compactionLayer='L0',
        compactedFromChars=9000, compactedToChars=None, rawToolTokens=2400,
        toolResultEvidence={'kind': 'tofu.tool-result-evidence/v1'},
        isError=False, status='error',
        rejection={'kind': 'user_rejected'},
        _rejected={'kind': 'user_rejected'},
        llmRound=2, tStart=1.0, tEnd=2.0,
    )
    assert validate_event(event) == ()


def test_undeclared_field_raises_at_construction():
    """★ THE INCIDENT FACE: rawToolTokens-before-declaration dies HERE, at
    the emitting line, not in a frontend badge."""
    with pytest.raises(EventContractError, match='undeclared field'):
        build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs(),
                    shinyNewUndeclaredField=1)


def test_missing_required_field_raises():
    kwargs = _valid_minimal_kwargs()
    del kwargs['toolContent']
    with pytest.raises(EventContractError, match='missing required field'):
        build_event(EventType.TOOL_COMPLETE, **kwargs)


@pytest.mark.parametrize('field_name,bad_value,good_value', [
    ('roundNum', '1', 1),
    ('toolCallId', 7, 'tc-1'),
    ('toolTokens', '120', 120),
    # bool is NOT an int on the wire (JSON semantics), and vice versa.
    ('toolTokens', True, 120),
    ('isError', 1, True),
    ('toolResultEvidence', 'evidence', {}),
    ('rejection', ['kind'], {}),
    ('tStart', 'now', 1.0),
])
def test_type_mismatch_raises(field_name, bad_value, good_value):
    bad_kwargs = {**_valid_minimal_kwargs(), field_name: bad_value}
    with pytest.raises(EventContractError, match=field_name):
        build_event(EventType.TOOL_COMPLETE, **bad_kwargs)
    good_kwargs = {**_valid_minimal_kwargs(), field_name: good_value}
    event = build_event(EventType.TOOL_COMPLETE, **good_kwargs)
    assert validate_event(event) == ()


def test_none_union_fields_accept_null_and_int():
    for value in (None, 9000):
        event = build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs(),
                            compactedFromChars=value)
        assert validate_event(event) == ()


def test_unschema_d_events_stay_permissive():
    """Events with no field-level contract keep the forward-compatible wire:
    no field gate. Uses an unregistered type so the pin survives the schema
    migration completing (a registered-but-unmigrated type is a moving target
    by design)."""
    event = build_event('never_registered_probe', anythingGoes=[1, 2])
    assert validate_event(event) == ()


def test_warn_mode_logs_instead_of_raising(monkeypatch, caplog):
    monkeypatch.setenv('TOFU_EVENT_SCHEMA', 'warn')
    with caplog.at_level(logging.WARNING):
        build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs(),
                    undeclared=1)  # must NOT raise
    assert any('wire contract violation' in record.message
               for record in caplog.records)


def test_off_mode_is_silent(monkeypatch, caplog):
    monkeypatch.setenv('TOFU_EVENT_SCHEMA', 'off')
    with caplog.at_level(logging.WARNING):
        build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs(),
                    undeclared=1)
    assert not any('wire contract violation' in record.message
                   for record in caplog.records)


def test_violation_listeners_observe_in_any_mode(monkeypatch):
    monkeypatch.setenv('TOFU_EVENT_SCHEMA', 'off')
    seen = []

    def listener(event, violations):
        seen.append((event, violations))

    add_event_violation_listener(listener)
    try:
        build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs(),
                    undeclared=1)
    finally:
        # Listeners are global; never leak one into the rest of the suite.
        remove_event_violation_listener(listener)
    assert len(seen) == 1
    assert seen[0][0]['type'] == EventType.TOOL_COMPLETE
    assert 'undeclared field' in seen[0][1][0]


# ── B2. Full-coverage round-trip — every schema'd event constructs ──

_FIELD_SAMPLES = {
    'str': 's',
    'int': 1,
    'number': 1.5,
    'bool': True,
    'dict': {},
    'list': [],
}


def _sample_for_kind(kind: str):
    for alternative in kind.split('|'):
        alternative = alternative.strip()
        if alternative == 'None':
            continue
        return _FIELD_SAMPLES[alternative]
    return None


def _kwargs_for(spec, *, required_only: bool) -> dict:
    # emittedAt is auto-stamped by build_event; never passed by emitters.
    return {f.name: _sample_for_kind(f.kind)
            for f in spec.schema
            if f.name != 'emittedAt' and (f.required or not required_only)}


def test_all_schema_d_events_roundtrip_required_only():
    """Every schema'd event must construct from its required fields alone —
    a required field with no valid sample means the schema is unemittable."""
    for spec in _schema_specs():
        event = build_event(spec.type, **_kwargs_for(spec, required_only=True))
        assert validate_event(event) == (), spec.type


def test_all_schema_d_events_roundtrip_full():
    """Every declared optional field must also pass the gate when present —
    a typo'd optional kind would otherwise stay invisible until emitted."""
    for spec in _schema_specs():
        event = build_event(spec.type, **_kwargs_for(spec, required_only=False))
        assert validate_event(event) == (), spec.type


# ═══════════════════════════════════════════════════════════════════
#  C. REAL pipeline conformance — scripted execute_tool_pipeline round
# ═══════════════════════════════════════════════════════════════════
#  Harness mirrors tests/test_tool_exec_failure_verdict.py (repo convention:
#  each suite carries its own self-contained harness).

def _mk_task(**over):
    task = {
        'id': 'schema-task-1',
        'convId': 'cv-schema-1',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'model': 'test-model',
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
        '_attended': False,
    }
    task.update(over)
    return task


def _mk_tc(tc_id: str, fn_name: str, seq: int):
    """A parsed_tcs 7-tuple through the REAL round constructor."""
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    _n, round_entry, _ev = _build_tool_round_entry(
        fn_name, {}, tc_id, '{}', seq, False)
    tc = {'id': tc_id, 'type': 'function',
          'function': {'name': fn_name, 'arguments': '{}'}}
    return (tc, fn_name, tc_id, {}, round_entry['roundNum'], round_entry, None)


class _Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, task, event):
        with self._lock:
            self.events.append(dict(event))

    def of_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e.get('type') == event_type]


@pytest.fixture()
def rec(monkeypatch):
    recorder = _Recorder()
    import lib.tasks_pkg.tool_dispatch._heartbeat as facade
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    from lib.tasks_pkg.executor import _finalize as exec_finalize
    monkeypatch.setattr(_pipeline, 'append_event', recorder, raising=False)
    monkeypatch.setattr(facade, 'append_event', recorder, raising=False)
    monkeypatch.setattr(exec_finalize, 'append_event', recorder, raising=False)
    return recorder


@pytest.fixture()
def scripted_tools(monkeypatch):
    script: dict[str, tuple] = {}

    def _fake(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
              cfg, project_path, project_enabled, all_tools=None):
        spec = script.get(fn_name, ('ok', 'ok'))
        if spec[0] == 'raise':
            raise spec[1]
        from lib.tasks_pkg.executor._finalize import _finalize_tool_round
        _finalize_tool_round(
            task, rn, round_entry,
            [{'toolName': fn_name, 'title': fn_name,
              'snippet': str(spec[1])[:60], 'source': 'Test',
              'fetched': True, 'fetchedChars': len(str(spec[1]))}])
        return tc_id, spec[1], False

    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', _fake, raising=False)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', _fake, raising=False)
    return script


def _run_pipeline(task, tcs):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    return execute_tool_pipeline(
        task, tcs, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=[],
        all_search_results_text=[], round_num=0, model='test-model')


def test_real_pipeline_emits_only_conforming_tool_events(rec, scripted_tools):
    """★ CONFORMANCE CIRCUIT: construct → mutate → deliver, all validated on
    the final recorded frame — covering BOTH the success lane (no status)
    and the failure lane (status='error'), which build the event differently.
    """
    scripted_tools['read_files'] = ('ok', 'FILE BODY')
    scripted_tools['explode_tool'] = ('raise', RuntimeError('boom'))

    task = _mk_task()
    tcs = [_mk_tc('tc-ok', 'read_files', 1),
           _mk_tc('tc-bad', 'explode_tool', 2)]
    _run_pipeline(task, tcs)

    completes = rec.of_type(EventType.TOOL_COMPLETE)
    assert len(completes) == 2, (
        f'expected one tool_complete per lane, got {completes!r}')
    violations = []
    for event in rec.events:
        violations.extend(
            f"{event.get('type')}: {violation}"
            for violation in validate_event(event))
    assert not violations, 'real pipeline emitted non-conforming frames:\n' \
        + '\n'.join(violations)

    by_call = {e['toolCallId']: e for e in completes}
    # Success lane: verdict ABSENT (the client must never see a status on a
    # clean completion); failure lane: verdict present and typed.
    assert 'status' not in by_call['tc-ok']
    assert by_call['tc-bad']['status'] == 'error'
    for event in completes:
        assert isinstance(event['tStart'], (int, float))
        assert isinstance(event['tEnd'], (int, float))
        assert isinstance(event['emittedAt'], (int, float))


def test_real_pipeline_undeclared_field_would_fail(rec, scripted_tools,
                                                   monkeypatch):
    """Guard ON the guard: if the pipeline ever stamps an undeclared field
    (the rawToolTokens regression), the construction gate raises — prove the
    gate is actually wired into this harness, not passing vacuously."""
    monkeypatch.setenv('TOFU_EVENT_SCHEMA', 'strict')
    with pytest.raises(EventContractError, match='undeclared field'):
        build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs(),
                    rawToolTokens2=1)


# ═══════════════════════════════════════════════════════════════════
#  D. Delivery seam — append_event re-checks the final frame
# ═══════════════════════════════════════════════════════════════════

def test_append_event_reports_post_mutation_violations(monkeypatch):
    """A frame built conforming and then mutated with an undeclared field is
    caught at the delivery seam (listener), which construction cannot see."""
    monkeypatch.setenv('TOFU_EVENT_SCHEMA', 'warn')
    seen = []
    listener = lambda event, violations: seen.append(violations)  # noqa: E731
    add_event_violation_listener(listener)
    task = _mk_task()
    try:
        from lib.tasks_pkg.manager import append_event
        frame = build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs())
        frame['sneakyMutation'] = True  # post-construction drift
        append_event(task, frame)
    finally:
        remove_event_violation_listener(listener)
    assert any('undeclared field' in violation
               for violations in seen for violation in violations), (
        f'delivery seam missed the mutated field: {seen!r}')


def test_check_event_validates_final_shape_without_raising_in_warn(
        monkeypatch):
    monkeypatch.setenv('TOFU_EVENT_SCHEMA', 'warn')
    frame = build_event(EventType.TOOL_COMPLETE, **_valid_minimal_kwargs())
    frame['status'] = 'error'  # declared mutation: conforming
    check_event(frame)  # must not raise, must not warn
    frame['bogus'] = 1
    seen = []
    listener = lambda event, violations: seen.append(violations)  # noqa: E731
    add_event_violation_listener(listener)
    try:
        check_event(frame)
    finally:
        remove_event_violation_listener(listener)
    assert seen and 'undeclared field' in seen[0][0]


def test_swarm_shape_conforms():
    """The swarm agent's tool_complete kwargs (incl. llmRound — the field the
    chat pipeline never sends) must conform, or the gate would raise on
    every sub-agent completion."""
    frame = build_event(
        EventType.TOOL_COMPLETE,
        roundNum=2, llmRound=2, toolCallId='tl-1', toolName='web_search',
        toolContent='RESULT', isError=False, tEnd=int(time.time() * 1000),
        status='error',
    )
    assert validate_event(frame) == ()


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
