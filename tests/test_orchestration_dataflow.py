"""Direct contracts for orchestration runtime Typed-I/O state."""

from pathlib import Path

import pytest

from lib.orchestration_dataflow import (
    OrchestrationDataflow,
    build_change_manifest,
)

pytestmark = pytest.mark.unit


def _role(node_id: str, *, inputs=None, outputs=None) -> dict:
    io_contract = {}
    if inputs is not None:
        io_contract['inputs'] = inputs
    if outputs is not None:
        io_contract['outputs'] = outputs
    params = {'io': io_contract} if io_contract else {}
    return {
        'id': node_id,
        'type': 'role',
        'role': 'worker',
        'params': params,
    }


def test_implicit_output_and_snapshot_are_detached():
    dataflow = OrchestrationDataflow()
    dataflow.publish_outputs(_role('worker'), 'worker result', [], 0)

    first = dataflow.output_snapshot()
    assert first == {'worker': {'text': 'worker result'}}
    first['worker']['text'] = 'mutated'
    assert dataflow.output_snapshot()['worker']['text'] == 'worker result'


def test_named_artifact_output_uses_one_deterministic_manifest():
    dataflow = OrchestrationDataflow()
    producer = _role('worker', outputs=[
        {'name': 'summary', 'type': 'text'},
        {'name': 'changes', 'type': 'artifact'},
        {'name': 'files', 'type': 'file'},
    ])
    dataflow.publish_outputs(
        producer,
        'human-readable result',
        ['write_file', 'apply_diff', 'write_file'],
        2,
    )

    outputs = dataflow.output_snapshot()['worker']
    assert outputs['summary'] == 'human-readable result'
    assert outputs['changes'] == outputs['files']
    assert 'write_file ×2' in outputs['changes']
    assert 'apply_diff' in outputs['changes']
    assert '2 exploratory' in outputs['changes']


def test_inputs_resolve_seed_named_and_primary_outputs_in_declared_order():
    dataflow = OrchestrationDataflow()
    dataflow.set_initial_context('original request')
    dataflow.publish_outputs(
        _role('producer', outputs=[
            {'name': 'summary', 'type': 'text'},
            {'name': 'changes', 'type': 'artifact'},
        ]),
        'producer summary',
        ['write_file'],
        0,
    )
    consumer = _role('consumer', inputs=[
        {'name': 'request', 'type': 'text', 'from': 'start'},
        {'name': 'primary', 'type': 'text', 'from': 'producer'},
        {'name': 'manifest', 'type': 'artifact', 'from': 'producer.changes'},
        {'name': 'missing', 'type': 'text', 'from': 'unknown'},
    ])

    context = dataflow.compose_inputs(consumer)
    assert context is not None
    assert context.index('## request') < context.index('## primary')
    assert context.index('## primary') < context.index('## manifest')
    assert 'original request' in context
    assert 'producer summary' in context
    assert 'Change manifest' in context
    assert '## missing' not in context


def test_none_distinguishes_legacy_mode_from_unresolved_strict_inputs():
    dataflow = OrchestrationDataflow()
    assert dataflow.compose_inputs(_role('legacy')) is None
    assert dataflow.compose_inputs(_role('strict', inputs=[
        {'name': 'missing', 'type': 'text', 'from': 'ghost'},
    ])) == ''


def test_empty_change_manifest_reports_exploration_without_fake_changes():
    manifest = build_change_manifest([], 3)
    assert 'no state-changing actions' in manifest
    assert '3 exploratory calls' in manifest


def test_engine_uses_dataflow_port_without_shadow_store_or_helpers():
    root = Path(__file__).resolve().parents[1]
    engine = (root / 'lib' / 'orchestration_engine.py').read_text()
    runtime = (root / 'lib' / 'orchestration_role_runtime.py').read_text()
    dataflow = (root / 'lib' / 'orchestration_dataflow.py').read_text()

    assert 'OrchestrationDataflow(lock=self._lock)' in engine
    assert 'self._dataflow.compose_inputs(' in runtime
    assert 'self._dataflow.publish_outputs(' in runtime
    assert '_io_outputs' not in engine
    assert 'def _publish_outputs(' not in engine
    assert 'def _compose_typed_inputs(' not in engine
    assert 'class OrchestrationDataflow' in dataflow
    assert 'FlowExecutor' not in dataflow
    assert engine.count('\n') < 1480
