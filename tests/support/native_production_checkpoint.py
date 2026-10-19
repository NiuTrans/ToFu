"""Trusted test composition of selected storage and native file recovery.

The optional hold injects process loss after file publication but before the
completed checkpoint ACK. Every journal transition, effect proof, budget and
resume lineage check remains in its production owner.
"""
import asyncio
import json
from pathlib import Path
import uuid


def file_checkpoint_factory(values):
    directory = Path(values['directory'])

    def factory(request, principal, executor, configuration):
        from lib.storage.service import get_storage_client
        from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
        from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery

        task = request.task
        resuming = bool(task.get('config', {}).get('_resumeSource'))

        async def restore(callback, state):
            # Observation only: shared native policies restore paid accounting,
            # tool consumption and original producer evidence themselves.
            (directory / 'restored-checkpoint.json').write_text(json.dumps(state, indent=2))

        class CrashCheckpoint(NativeCheckpointPolicy):
            async def complete(self, callback):
                if values.get('crash_pending') and not resuming:
                    from lib.project_mod.modifications import _get_session_dir
                    base = task['toolRounds'][0]['executionEffect']['reconciliation']['basePath']
                    receipt = {'task_id': task['id'], 'turn_id': task['_turnId'],
                        'attempt_id': task['_attemptId'], 'phase': 'published_before_checkpoint_ack',
                        'consumption': task['_executionBudget'].consumption_snapshot(),
                        'tool_rounds': task.get('toolRounds', []),
                        'fee_rounds': task.get('apiRounds', []),
                        'file_evidence_directory': _get_session_dir(base, create=False)}
                    target = directory / 'pending-file.json'
                    temporary = target.with_suffix('.tmp')
                    temporary.write_text(json.dumps(receipt, indent=2))
                    temporary.replace(target)
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=60)
                return await super().complete(callback)

        return CrashCheckpoint(principal=principal, task=task,
            storage=get_storage_client(write=True), executor=executor,
            epoch='turn:' + task['_turnId'], binding='production-file-recovery-v1',
            token=uuid.uuid4().hex, header_bytes=4096, snapshot_bytes=8 << 20,
            identity_bytes=1024, io_timeout_ms=30000, restore=restore,
            handoff_from_accepted=resuming,
            tool_recovery=NativeFileToolRecovery(principal=principal, task=task, executor=executor,
                allow_unreconciled_batches=bool(values.get('unreconciled_batches'))))

    return factory
