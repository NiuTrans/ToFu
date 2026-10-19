"""Disposable Flow writer parked after child settlement, before Flow ACK.

The test parent owns storage and kills this process group. Credentials arrive
only on stdin; the ready file contains public producer coordinates and metrics.
The real file tool runs through ordinary registered-child effect persistence.
"""
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace


def main():
    import pytest
    from lib.storage.client import StorageClient
    from lib.storage.v2_client import StorageV2Client
    from lib import turn_lifecycle, file_history
    from lib.project_mod import modifications
    from lib.project_mod.write_tools import _ops
    from lib.orchestration.engine import FlowExecutor
    from lib.orchestration.execution_journal import ExecutionJournal
    from lib.orchestration.invocation_recovery import FIELD
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from tests.orchestration.test_orchestration_execution_recovery import graph, role
    from tests.turn_lifecycle.test_task_event_delivery import _task
    from tests.turn_lifecycle.test_child_file_effects import write
    import lib.tasks_pkg.child_invocation as children
    values = json.load(sys.stdin)
    client = (StorageV2Client(*values['endpoint'], values['token'].encode(),
        owner_id=values['owner_id'], tenant_id=values['tenant_id']) if values['native']
        else StorageClient(*values['endpoint'], values['token']))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('lib.storage.get_storage_client', lambda write=False: client)
        patch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
        patch.setattr(modifications, 'SESSIONS_DIR', values['journal'])
        patch.setattr(file_history, 'track_edit', lambda *a, **k: None)
        patch.setattr(_ops, '_touch_for_vscode', lambda *a, **k: None)
        _, parent = _task(turn_lifecycle, SimpleNamespace(client=client), 'killed-flow')
        parent['config']['projectPath'] = values['project']
        store = SidecarOrchestrationRunStore(1, client=lambda **_: client)
        definition, events, identity = graph(role('worker')), [], {}
        run_id = store.new_run_id()
        assert store.create_run(run_id, definition=definition, input_text='input')
        def emit(event):
            assert store.append_event(run_id, len(events) + 1, event)
            events.append(event)
        def execute(child, *, policy):
            row, result = write(child, Path(values['project']))
            assert row['status'] == 'done', result
            identity.update(invocation_id=child['id'], operation_id=row['executionEffect']['operationId'])
            child.update(status='done', finishReason='stop', streamState='provider_finished',
                         content='verified once\n' + 'x' * values.get('result_bytes', 0))
            if values.get('native_fees'):
                from lib.billing.cost import transfer_native_child_fees
                from tests.billing.test_native_actor_fee_evidence import paid
                child.update(status='running', apiRounds=[], _nativePricingRequired=True, _executionActualsComplete=True)
                paid(child)
                child['status'] = 'done'
                transfer_native_child_fees(parent, child)
        patch.setattr(children, 'execute_invocation', execute)
        command = client.command
        def checkpoint(operation, payload, *args, **kwargs):
            started = time.perf_counter()
            result = command(operation, payload, *args, **kwargs)
            if operation == 'task_results.checkpoint' and FIELD in payload.get('value', {}):
                receipt = {**identity, 'run_id': run_id, 'conv_id': parent['convId'],
                    'definition': definition, 'parent_task_id': parent['id'],
                    'native_fees': payload['value'][FIELD].get('native_fees'),
                    'completion_bytes': len(json.dumps(payload['value'][FIELD]).encode()),
                    'commit_ms': (time.perf_counter() - started) * 1000}
                ready = Path(values['ready']); temporary = ready.with_suffix('.pending')
                temporary.write_text(json.dumps(receipt) + '\n'); os.replace(temporary, ready)
                while True:
                    signal.pause()
            return result
        patch.setattr(client, 'command', checkpoint)
        journal = ExecutionJournal(emit)
        journal.begin(definition, 'input')
        FlowExecutor(definition, parent_task=parent, model='fixture', project_path=values['project'],
            model_routing_policy='selected', on_event=emit,
            execution_journal=journal).run(initial_context='input')
        raise AssertionError('Crash probe unexpectedly returned')


if __name__ == '__main__':
    main()
