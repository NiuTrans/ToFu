"""Fixture-only Sidecar phase tracing over the ordinary owned child lifecycle.

The optional benchmark launcher substitutes only the Python module argv. The
Sidecar retains its stdin/stdout ownership, auth, storage and shutdown contracts.
No production module loads this profiler. Labels contain operation names only.
"""
from contextlib import ExitStack, contextmanager
from functools import wraps
import runpy
import subprocess
from unittest.mock import patch

from benchmarks.native_application_stages import Stages


@contextmanager
def diagnostic_sidecar_launch():
    original = subprocess.Popen
    def launch(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and list(command[1:3]) == ['-m', 'lib.storage_authority_sql']:
            command = [command[0], '-m', 'benchmarks.native_sidecar_stages', *command[3:]]
        return original(command, *args, **kwargs)
    with patch.object(subprocess, 'Popen', launch):
        yield


def install_sidecar_stages(stack):
    from lib.storage_authority_sql import server
    from lib.storage_authority_sql.adapters import sqlite
    stages = Stages()
    from lib.storage_authority_sql.operations_pkg import _turns_write, _turns_core
    for owner, name, label in [
        (_turns_write, 'projection_from_turn_row', 'sidecar.start_projection'),
        (_turns_write, 'build_projection_patch', 'sidecar.start_patch'),
        (_turns_write, '_turn_event_append', 'sidecar.start_event'),
        (_turns_core, '_append_conversation_change', 'sidecar.sync_change'),
        (_turns_core, '_committed_event_notice', 'sidecar.sync_notice'),
    ]:
        stack.enter_context(patch.object(owner, name, stages.sync(label, getattr(owner, name))))
    def operation_span(owner, name, prefix, position):
        original = getattr(owner, name)
        @wraps(original)
        def call(*args, **kwargs):
            operation = args[position] if len(args) > position else kwargs.get('operation_name', '')
            if name == '_dispatch':
                operation = operation.get('operation', '')
            elif name == '_transaction':
                operation = operation.operation_name
            with stages.span(prefix + str(operation)[:128]):
                return original(*args, **kwargs)
        stack.enter_context(patch.object(owner, name, call))
    operation_span(server._StorageHandler, '_dispatch', 'sidecar.dispatch.', 1)
    operation_span(server._StorageHandler, '_send_response', 'sidecar.response.', 3)
    operation_span(sqlite.SQLiteBackend, 'command', 'sidecar.command.', 1)
    operation_span(sqlite._FairWriter, '_transaction', 'sidecar.transaction.', 1)
    import builtins
    original_import=builtins.__import__
    def traced_import(name,*args,**kwargs):
        if name in {'jsonschema','lib.automations.events'}:
            with stages.span('sidecar.import.'+name):
                return original_import(name,*args,**kwargs)
        return original_import(name,*args,**kwargs)
    stack.enter_context(patch.object(builtins,'__import__',traced_import))
    profiles=[]
    resolve = server.resolve_operation_contract
    @wraps(resolve)
    def contract(operation, *args, **kwargs):
        with stages.span('sidecar.resolve.' + operation):
            receipt, callback, timeout = resolve(operation, *args, **kwargs)
        measured=stages.sync('sidecar.execute.' + operation, callback)
        if operation == 'turn.attempt.start':
            original_measured=measured
            def measured(*args, **kwargs):
                import cProfile, pstats
                from pathlib import Path
                profile=cProfile.Profile()
                profile.enable()
                try:
                    return original_measured(*args, **kwargs)
                finally:
                    profile.disable()
                    rows=[]
                    for (file,line,name),(primitive,calls,own,total,_) in pstats.Stats(profile).stats.items():
                        rows.append(dict(file='/'.join(Path(file).parts[-3:]),line=line,name=name,
                            primitive_calls=primitive,calls=calls,own_seconds=own,cumulative_seconds=total))
                    profiles.append(sorted(rows,key=lambda row:row['cumulative_seconds'],reverse=True)[:100])
        return receipt, measured, timeout
    stack.enter_context(patch.object(server, 'resolve_operation_contract', contract))
    original_metrics = sqlite.SQLiteBackend.metrics
    def metrics(owner):
        result = original_metrics(owner)
        result['diagnostic_sidecar_stages'] = stages.snapshot()
        result['diagnostic_sidecar_origin_ns'] = stages.origin
        result['diagnostic_sidecar_cprofile'] = list(profiles)
        return result
    stack.enter_context(patch.object(sqlite.SQLiteBackend, 'metrics', metrics))
    return stages


if __name__ == '__main__':
    with ExitStack() as stack:
        install_sidecar_stages(stack)
        runpy.run_module('lib.storage_authority_sql', run_name='__main__')
