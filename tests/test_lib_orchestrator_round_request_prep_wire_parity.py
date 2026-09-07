"""Wire-parity guards for pt_03f4cdf1 slice 28 — extract the
round-request preamble cluster from _run.py's stream loop into
lib.tasks_pkg.orchestrator._round_request_prep.build_round_request().

The cluster runs once per stream round after inbox drain and before
the streaming-tool accumulator construction:

    1. Reuse the stable tool list on every round,
    2. Cache-aware tool-result ordering: sort consecutive tool results
       by tool_call_id so the prefix is deterministic across rounds
       (automatic prefix caching on OpenAI/Qwen),
    3. Emit the messages-snapshot debug event (AFTER the sort so the
       panel reflects the real outbound ordering),
    4. Build the request body through the explicit ``_ports`` dependency,
    5. Attach ``body['_task_id']`` for the session-stable TTL latch in
       add_cache_breakpoints (prevents mid-session cache key shift).

It returns ``(_tools_this_round, body)`` — the tool list is still
needed downstream by the round-checkpoint call (slice 20).

Failing-first: written BEFORE the extraction; the module/signature/
delegation guards turn RED until the leaf exists and _run.py
delegates.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_run.py'
ROOT_LOOP_PY = (
    ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_root_agent_loop.py')
LEAF_PY = ROOT / 'lib' / 'tasks_pkg' / 'orchestrator' / '_round_request_prep.py'


# ---------------------------------------------------------------------------
# 1. leaf module exists and exposes the helper by name
# ---------------------------------------------------------------------------
def test_leaf_module_exists_and_exposes_prep_helper():
    """The new leaf ships a single top-level callable named
    ``build_round_request`` — the seam name run_task will delegate to.
    Deleting the leaf or renaming the callable must break a downstream
    import."""
    mod = importlib.import_module(
        'lib.tasks_pkg.orchestrator._round_request_prep')
    assert hasattr(mod, 'build_round_request'), (
        'lib.tasks_pkg.orchestrator._round_request_prep must export '
        'build_round_request')
    assert callable(mod.build_round_request)


# ---------------------------------------------------------------------------
# 2. helper signature (positional carriers + kw-only scalars)
# ---------------------------------------------------------------------------
def test_prep_helper_signature():
    """The helper takes ``task, rs, messages, tool_list`` positional and
    the scalars keyword-only. Any drift breaks _run.py's call site and
    this test."""
    import inspect
    from lib.tasks_pkg.orchestrator._round_request_prep import (
        build_round_request)
    sig = inspect.signature(build_round_request)
    params = sig.parameters
    for name in ('task', 'rs', 'messages', 'tool_list'):
        assert name in params, f'{name} must be a parameter'
    for name in ('round_num', 'tid', 'thinking_depth',
                 'temperature', 'max_tokens', 'response_format',
                 'admitted_input_tokens', 'admitted_tool_schema_tokens',
                 'admitted_tool_schema_fingerprint',
                 'reusable_text_token_counts_by_identity'):
        assert name in params, f'{name} must be a parameter'
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f'{name} must be keyword-only')


# ---------------------------------------------------------------------------
# 3. _run.py imports and delegates to the extracted helper
# ---------------------------------------------------------------------------
def test_run_py_imports_prep_helper():
    """The root loop adapter imports build_round_request."""
    src = ROOT_LOOP_PY.read_text()
    assert 'from lib.tasks_pkg.orchestrator._round_request_prep import' in src, (
        '_run.py must import the extracted prep helper — expected a '
        '`from lib.tasks_pkg.orchestrator._round_request_prep import ...` '
        'line at module scope')
    assert 'build_round_request' in src, (
        '_run.py must reference build_round_request (either in the import '
        'or in the call site)')


def test_run_task_delegates_to_prep_helper():
    """The stream loop's preamble must unpack the 2-tuple from a single
    call to ``build_round_request(...)`` — no inline body left behind."""
    src = ROOT_LOOP_PY.read_text()
    assert 'tools_this_round, body = build_round_request(' in src, (
        'the adapter must unpack `tools_this_round, body = '
        'build_round_request(...)` in the stream loop')


# ---------------------------------------------------------------------------
# 4. inline bodies are gone from _run.py (extraction really happened)
# ---------------------------------------------------------------------------
def test_run_py_no_longer_sorts_tool_results_inline():
    src = RUN_PY.read_text()
    assert 'sort_tool_results(' not in src


def test_run_py_no_longer_builds_body_inline():
    """The request-body dependency belongs to the extracted leaf."""
    src = RUN_PY.read_text()
    assert 'build_request_body(' not in src


def test_run_py_no_longer_attaches_task_id_inline():
    src = RUN_PY.read_text()
    assert "body['_task_id'] = task['id']" not in src, (
        "the body['_task_id'] = task['id'] assignment must live in "
        '_round_request_prep.py, not _run.py (the bare mention in the '
        'slice-28 pointer comment is fine)')


def test_run_py_has_no_tool_round_gate():
    src = RUN_PY.read_text()
    assert 'max_tool_rounds' not in src


# ---------------------------------------------------------------------------
# 5. leaf carries the pivotal semantics (order + late binding + attach)
# ---------------------------------------------------------------------------
def test_leaf_preserves_step_ordering():
    """Build once, then snapshot the canonical body messages."""
    src = LEAF_PY.read_text()
    i_snap = src.index('emit_messages_snapshot_event(')
    i_body = src.index('orchestrator_ports.build_request_body(')
    assert i_body < i_snap, (
        'leaf must order build_request_body → '
        'emit_messages_snapshot_event so the snapshot reuses canonical body '
        'messages instead of sanitizing the prompt twice')
    assert 'sort_tool_results(' not in src


def test_leaf_builds_body_through_the_explicit_port_owner():
    """All phases must share the concrete, patchable dependency owner."""
    src = LEAF_PY.read_text()
    assert 'import lib.tasks_pkg.orchestrator._ports as orchestrator_ports' in src
    assert 'orchestrator_ports.build_request_body(' in src


def test_leaf_attaches_task_id_for_cache_ttl():
    """The leaf MUST attach body['_task_id'] — the session-stable TTL
    latch in add_cache_breakpoints prevents mid-session cache key
    shift."""
    src = LEAF_PY.read_text()
    assert "body['_task_id'] = task['id']" in src, (
        "leaf must attach body['_task_id'] = task['id'] (cache-TTL latch)")


def test_leaf_reuses_tools_on_every_round():
    """The model sees the stable tool surface until natural completion."""
    src = LEAF_PY.read_text()
    assert '_tools_this_round = tool_list' in src
    assert 'max_tool_rounds' not in src


def test_leaf_returns_two_tuple():
    """The leaf returns (_tools_this_round, body) — the tool list is
    still needed downstream by the round-checkpoint call."""
    src = LEAF_PY.read_text()
    assert 'return _tools_this_round, body' in src, (
        'leaf must `return _tools_this_round, body`')


def test_leaf_passes_call_local_admission_count_to_body_builder(monkeypatch):
    from types import SimpleNamespace

    import lib.tasks_pkg.orchestrator._round_request_prep as prep
    import lib.context_telemetry as context_telemetry

    captured = {}
    captured_telemetry = {}
    captured_snapshots = []
    monkeypatch.setattr(
        prep, 'emit_messages_snapshot_event',
        lambda *args, **kwargs: captured_snapshots.append((args, kwargs)))
    monkeypatch.setattr(
        context_telemetry,
        'capture_round_context',
        lambda *args, **kwargs: captured_telemetry.update(kwargs),
    )

    def build_body(model, messages, **kwargs):
        captured.update(kwargs)
        return {
            'model': model,
            'messages': list(messages),
            'tools': kwargs.get('tools'),
        }

    monkeypatch.setattr(
        prep.orchestrator_ports, 'build_request_body', build_body)
    state = SimpleNamespace(
        model='gpt-5.6-sol', preset='medium', thinking_enabled=True)
    task = {
        'id': 'admission-reuse', 'convId': 'conv-reuse',
        '_userId': 1, 'config': {},
    }

    tools = [{'type': 'function', 'function': {'name': 'read_files'}}]
    _, body = prep.build_round_request(
        task,
        state,
        [{'role': 'user', 'content': 'hello'}],
        tools,
        round_num=0,
        tid='admission',
        thinking_depth='medium',
        temperature=1.0,
        max_tokens=4096,
        response_format=None,
        admitted_input_tokens=111_000,
        admitted_tool_schema_tokens=18_000,
        admitted_tool_schema_fingerprint='a' * 64,
        reusable_text_token_counts_by_identity={123: 456},
    )

    assert captured['precomputed_input_tokens'] == 111_000
    assert captured_snapshots[0][1]['prepared_messages'] is body['messages']
    from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY
    assert body[ADMITTED_INPUT_TOKENS_KEY] == 111_000
    assert captured_telemetry['precomputed_tool_schema_tokens'] == 18_000
    assert captured_telemetry[
        'reusable_text_token_counts_by_identity'] == {123: 456}
    evidence = body[context_telemetry.TOOL_SCHEMA_EVIDENCE_KEY]
    assert context_telemetry.reusable_tool_schema_token_count(
        evidence,
        list(tools),
        model=state.model,
    ) == 18_000
    assert context_telemetry.tool_schema_fingerprint_from_evidence(
        evidence) == 'a' * 64


def test_orchestration_schema_decision_is_latched_and_additive(monkeypatch):
    from types import SimpleNamespace

    import lib.context_telemetry as context_telemetry
    import lib.tasks_pkg.orchestrator._round_request_prep as prep
    import lib.tasks_pkg.tool_orchestration_policy as policy

    decisions = []

    def resolve(**kwargs):
        decisions.append(kwargs['round_num'])
        return {
            'policyVersion': 'tool-orchestration/v2',
            'compositionMode': 'ptc_bounded_reduction',
            'programmaticCalling': 'on',
            'programmaticReason': 'resident_eligible_read_tools',
            'programmaticTier': 'program',
            'programmaticExposurePolicy': 'serial_gateway',
            'programmaticSerialChainLength': 0,
            'programmaticSerialChain': [],
            'programmaticStage': 'stable stage',
            'programmaticEligibleTools': ['read_files'],
            'multiAgent': 'off',
            'multiAgentReason': 'disabled',
            'multiAgentStage': '',
            'round': kwargs['round_num'],
            'shape': 'ptc_bounded_reduction',
            'expectedSavings': {},
            'projectionEvidence': [],
            'adoptionEvidence': [],
        }

    monkeypatch.setattr(policy, 'resolve_tool_orchestration', resolve)
    monkeypatch.setattr(prep, 'emit_messages_snapshot_event', lambda *a, **k: None)
    monkeypatch.setattr(context_telemetry, 'capture_round_context', lambda *a, **k: None)
    monkeypatch.setattr(
        prep.orchestrator_ports, 'build_request_body',
        lambda model, messages, **kwargs: {
            'model': model, 'messages': list(messages),
            'tools': kwargs.get('tools'),
        },
    )
    task = {
        'id': 'stable-tools', 'convId': 'stable-tools-conv', '_userId': 1,
        'config': {'tools': {'programmaticExposure': 'serial_gateway'}},
        '_toolScriptBatchFallback': True,
    }
    state = SimpleNamespace(
        model='test-model', preset='default', thinking_enabled=False,
    )
    tools = [{'type': 'function', 'function': {'name': 'read_files'}}]
    kwargs = dict(
        tid='stable', thinking_depth='medium', temperature=1.0,
        max_tokens=4096, response_format=None,
    )

    _, first = prep.build_round_request(
        task, state, [{'role': 'user', 'content': 'inspect all files'}],
        tools, round_num=0, **kwargs,
    )
    _, second = prep.build_round_request(
        task, state, [{'role': 'user', 'content': 'inspect all files'}],
        tools, round_num=3, **kwargs,
    )

    assert decisions == [0]
    assert first['_programmatic_tier'] == second['_programmatic_tier'] == 'program'
    assert first['_programmatic_exposure'] == second['_programmatic_exposure'] == 'additive'
    assert task['_toolOrchestration']['round'] == 3


def test_body_build_failure_emits_fallback_snapshot_then_reraises(monkeypatch):
    from types import SimpleNamespace

    import lib.tasks_pkg.orchestrator._round_request_prep as prep

    snapshots = []
    monkeypatch.setattr(
        prep, 'emit_messages_snapshot_event',
        lambda *args, **kwargs: snapshots.append((args, kwargs)),
    )
    failure = RuntimeError('body construction failed')
    monkeypatch.setattr(
        prep.orchestrator_ports,
        'build_request_body',
        lambda *a, **k: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(RuntimeError) as raised:
        prep.build_round_request(
            {'id': 'failure-task', 'convId': 'failure-conv', '_userId': 1},
            SimpleNamespace(
                model='gpt-5.6-sol', preset='medium',
                thinking_enabled=False),
            [{'role': 'user', 'content': 'diagnose me'}],
            [],
            round_num=0,
            tid='failure',
            thinking_depth='medium',
            temperature=1.0,
            max_tokens=4096,
            response_format=None,
        )

    assert raised.value is failure
    assert len(snapshots) == 1
    assert 'prepared_messages' not in snapshots[0][1]
