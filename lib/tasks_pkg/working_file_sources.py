"""Request-owned read_files witnesses, published only after successful settlement.

The task-local bank holds bounded metadata, not source bodies or durable truth.
Eviction/restart requires a new authorized read for a new quote. Existing sealed
facts survive independently in the selected store. JSON tool output is no grant.
"""
import os
import time

from lib.artifacts.file_snapshot import FileSnapshot
from lib.memory.working_facts.file_sources import FileWitness
from lib.memory.working_facts.repository import digest

from .working_source_scope import source_scope as _scope, source_receipt_lock as _bank_lock, source_task_is_live


def file_witness_resolver(task):
    scope = _scope(task)
    def resolve(source):
        if _scope(task) != scope or not source_task_is_live(task):
            return None
        with _bank_lock:
            witness = (task.get('_working_file_sources') or {}).get((source['id'], source['version']))
        if not isinstance(witness, FileWitness) or witness.scope != scope or witness.task_id != str(task.get('id') or ''):
            return None
        # New quotes still require the issuing task's current file authority.
        from lib.project_dispatch.scope import tool_scope
        with tool_scope(task, 'read_files', {'path': os.path.relpath(witness.path, witness.root)}, witness.root) as grant:
            if grant is not None:
                grant.check(os.path.relpath(witness.path, witness.root))
            else:
                from lib.tasks_pkg.workspace_delivery import ensure_execution_roots
                policy = ensure_execution_roots(task)
                if witness.root not in {os.path.abspath(row['path']) for row in (policy or {}).get('roots', {}).values()}:
                    return None
        return witness
    return resolve


class FileReadCapture:
    def __init__(self, task, call_id):
        from lib.tools.resource_policy import tool_result_cache_capacity
        self.task, self.scope = task, _scope(task)
        self.task_id, self.call_id = str(task.get('id') or ''), str(call_id)
        self.capacity = tool_result_cache_capacity()
        self.pending = {}
        from lib.memory.working_facts.contract import limit
        self.deadline = time.monotonic() + limit('acquisitionSeconds')

    def check(self):
        from lib.project_mod.tool_admission import cancellation
        control = cancellation()
        if not source_task_is_live(self.task) or (control is not None and control.is_set()):
            raise TimeoutError('File source read cancelled')
        if time.monotonic() >= self.deadline:
            raise TimeoutError('File source acquisition expired')
        if _scope(self.task) != self.scope or str(self.task.get('id') or '') != self.task_id:
            raise PermissionError('File source scope changed')

    def bind(self, snapshot, *, roots):
        self.check()
        if not isinstance(snapshot, FileSnapshot) or snapshot.root not in {
                os.path.abspath(root) for root in roots}:
            raise PermissionError('File source requires a verified execution root')
        identifier = 'file_' + digest([*self.scope, snapshot.root,
                                      snapshot.root_identity, snapshot.relative_path])
        witness = FileWitness(self.scope, identifier, snapshot.sha256, snapshot.path,
            snapshot.root, snapshot.root_identity, self.task_id, self.call_id)
        self.pending[(identifier, snapshot.sha256)] = witness
        while len(self.pending) > self.capacity:
            self.pending.pop(next(iter(self.pending)))
        return {'kind': 'file', 'id': identifier, 'version': snapshot.sha256, 'path': snapshot.path}

    def publish(self, round_entry):
        if round_entry.get('status') != 'done':
            return
        self.check()
        with _bank_lock:
            self.check()
            bank = self.task.setdefault('_working_file_sources', {})
            for key, witness in self.pending.items():
                bank.pop(key, None)
                bank[key] = witness
            while len(bank) > self.capacity:
                bank.pop(next(iter(bank)))
